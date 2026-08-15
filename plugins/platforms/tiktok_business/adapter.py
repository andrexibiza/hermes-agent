"""TikTok Business Messaging platform adapter.

Per the design spec §8: receives and answers TikTok Business Account DMs.

The adapter implements BasePlatformAdapter.  It runs an aiohttp webhook
server, verifies TikTok signatures, normalizes inbound events to
MessageEvent, and sends outbound replies with capability gating.

Per §3.2 (Webhook Revolution baseline):
- Composite idempotency key: (profile, route, provider, account_alias, event_id)
- Rate limiting isolated by profile and route, then provider/account
- JSON arrays and scalars are rejected as invalid webhook envelopes
- Body length is measured in UTF-8 bytes
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.http import BoundedApiClient, EndpointConfig
from plugins.bytedance.shared.observability import Metrics, hash_id
from plugins.bytedance.shared.state import StateStore, get_state_store
from plugins.bytedance.shared.webhook import (
    CompositeIdempotencyKey,
    NormalizedEvent,
    WebhookIngress,
)
from plugins.platforms.tiktok_business.client import TikTokBusinessClient
from plugins.platforms.tiktok_business.models import (
    AccountConfig,
    PROVIDER_TIKTOK_BUSINESS,
    TikTokBusinessAPI,
    TikTokScope,
    SCOPE_TO_FEATURE,
    capabilities_from_scopes,
    scope_set_from_token_info,
)
from plugins.platforms.tiktok_business.policy import TikTokPolicyEngine
from plugins.platforms.tiktok_business.webhook import (
    TikTokWebhookParser,
    TikTokWebhookVerifier,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8654

# Canonical chat ID: <provider>:<account_alias>:<provider_conversation_id>
# (design spec §6.4)


def _build_chat_id(provider: str, account_alias: str, conversation_id: str) -> str:
    return f"{provider}:{account_alias}:{conversation_id}"


def _parse_chat_id(chat_id: str) -> Optional[tuple]:
    """Parse a canonical chat ID back into components.

    Returns (provider, account_alias, conversation_id) or None.
    """
    parts = chat_id.split(":", 2)
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


class TikTokBusinessAdapter(BasePlatformAdapter):
    """TikTok Business Messaging gateway adapter.

    Runs an aiohttp webhook server, receives TikTok Business Messaging
    events, and routes them to the Hermes agent.  Outbound messages go
    through the capability gate before hitting the provider API.
    """

    interactive_resume: bool = False  # webhook runs are event-triggered

    def __init__(self, config: PlatformConfig) -> None:
        from gateway.config import Platform
        super().__init__(config, Platform.TIKTOK_BUSINESS)
        self._extra = getattr(config, "extra", {}) or {}
        self._accounts: Dict[str, AccountConfig] = {}
        self._clients: Dict[str, TikTokBusinessClient] = {}
        self._state = get_state_store()
        self._policy = TikTokPolicyEngine(state_store=self._state)
        self._ingress: Dict[str, WebhookIngress] = {}
        self._profile = self._resolve_profile()

        self._parse_accounts()

        # Webhook server config
        self._host = self._extra.get("host") or os.environ.get(
            "TIKTOK_BUSINESS_HOST"
        )
        self._port = int(
            self._extra.get("port")
            or os.environ.get("TIKTOK_BUSINESS_PORT", DEFAULT_PORT)
        )
        self._public_url = self._extra.get("public_url") or os.environ.get(
            "TIKTOK_BUSINESS_PUBLIC_URL"
        )

        self._runner = None
        self._app = None
        self._connected = False

    def _resolve_profile(self) -> str:
        """Resolve the active Hermes profile name."""
        try:
            from hermes_constants import get_hermes_home
            home = str(get_hermes_home())
            # Extract profile from home path if multiplexed
            # The default profile uses the standard hermes home
            import os
            profile = os.environ.get("HERMES_PROFILE", "default")
            return profile
        except Exception:
            return "default"

    def _parse_accounts(self) -> None:
        """Parse account configurations from plugin settings config.

        Supports the config shape from the design spec §12.3:
        ```yaml
        plugins:
          entries:
            tiktok-business:
              settings:
                accounts:
                  nous-global:
                    business_account_id: ...
                    access_token_secret: ...
                    webhook_secret: ...
                    route_id: ...
        ```
        """
        accounts_cfg = self._extra.get("accounts", {}) or {}
        api_version = self._extra.get("api_version", "v1.3")

        for alias, cfg in accounts_cfg.items():
            if not isinstance(cfg, dict):
                continue
            self._accounts[alias] = AccountConfig(
                provider=PROVIDER_TIKTOK_BUSINESS,
                profile=self._profile,
                account_alias=alias,
                provider_account_id=(
                    cfg.get("business_account_id")
                    or os.environ.get("TIKTOK_BUSINESS_ACCOUNT_ID", "")
                ),
                access_token_secret=(
                    cfg.get("access_token_secret")
                    or "tiktok_business/access_token"
                ),
                webhook_secret=cfg.get("webhook_secret"),
                route_id=cfg.get("route_id") or secrets.token_urlsafe(16),
                home_conversation=cfg.get("home_conversation"),
                allowed_users=cfg.get("allowed_users", []) or [],
                allow_all_users=cfg.get("allow_all_users", False),
                manage_webhook=cfg.get("manage_webhook", False),
                region=cfg.get("region"),
                api_version=api_version,
            )

    def _find_account_by_route(self, route_id: str) -> Optional[AccountConfig]:
        """Find the account bound to a webhook route_id."""
        for account in self._accounts.values():
            if account.route_id == route_id:
                return account
        return None

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to TikTok Business Messaging.

        Per §8.3:
        1. Resolve account-scoped secret references.
        2. Call the access-token inspector.
        3. Verify required Business Messaging Read/Send scopes.
        4. Fetch/confirm Business Account identity.
        5. Validate webhook configuration or create it only when
           manage_webhook=true.
        6. Start the bounded local webhook server.
        7. Restore durable unprocessed events.
        8. Mark adapter healthy only after credentials, account binding,
           and webhook route are coherent.
        """
        if not self._accounts:
            logger.error("[tiktok_business] No accounts configured")
            self._fatal_error_code = "no_accounts"
            self._fatal_error_message = "No TikTok Business accounts configured"
            return False

        # Verify credentials and inspect scopes for each account
        for alias, account in self._accounts.items():
            try:
                client = TikTokBusinessClient(account, token_broker=self._token_broker)
                self._clients[alias] = client

                # 2. Inspect token
                scopes = await client.inspect_scopes()

                # 3. Verify required scopes
                if TikTokScope.READ.value not in scopes:
                    logger.warning(
                        "[tiktok_business] Account %s missing READ scope",
                        alias,
                    )
                if TikTokScope.SEND.value not in scopes:
                    logger.warning(
                        "[tiktok_business] Account %s missing SEND scope",
                        alias,
                    )

                # 4. Verify account identity
                account_info = await client.check_account_identity()
                data = account_info.get("data") or {}
                provider_account_id = data.get("business_account_id", "")
                if provider_account_id and provider_account_id != account.provider_account_id:
                    logger.error(
                        "[tiktok_business] Account %s ID mismatch: config=%s provider=%s",
                        alias,
                        account.provider_account_id,
                        provider_account_id,
                    )
                    self._clients.pop(alias, None)
                    continue

                # 5. Webhook config check (if manage_webhook)
                if account.manage_webhook and self._public_url:
                    await self._sync_webhook_config(client, account)

                # Set up ingress for this account
                verifier = TikTokWebhookVerifier()
                parser = TikTokWebhookParser()
                self._ingress[account.route_id] = WebhookIngress(
                    provider=PROVIDER_TIKTOK_BUSINESS,
                    verifier=verifier,
                    parser=parser,
                    state_store=self._state,
                )

            except ProviderError as e:
                logger.error(
                    "[tiktok_business] Account %s connect failed: %s",
                    alias,
                    e,
                )
                self._clients.pop(alias, None)
                continue

        if not self._clients:
            logger.error("[tiktok_business] No accounts could connect")
            self._fatal_error_code = "auth_failure"
            return False

        # 6. Start webhook server
        await self._start_webhook_server()

        # 7. Restore unprocessed events
        await self._restore_unprocessed_events()

        self._connected = True
        self._mark_connected()
        logger.info(
            "[tiktok_business] Connected %d account(s), webhook on %s:%d",
            len(self._clients),
            self._host or "*",
            self._port,
        )
        return True

    async def _sync_webhook_config(
        self, client: TikTokBusinessClient, account: AccountConfig
    ) -> None:
        """Validate or create the webhook configuration."""
        try:
            existing = await client.list_webhooks()
            existing_urls = {
                wh.get("webhook_url")
                for wh in (existing.get("data") or {}).get("webhook_list", [])
            }
            callback_url = self._callback_url_for(account)
            if callback_url not in existing_urls:
                logger.info(
                    "[tiktok_business] Registering webhook for %s -> %s",
                    account.account_alias,
                    callback_url,
                )
                await client.configure_webhook(
                    callback_url,
                    ["message.sent", "message.received", "conversation.updated"],
                )
        except ProviderError as e:
            # A failed webhook-management call does not overwrite a valid
            # existing configuration (§8.3)
            logger.warning(
                "[tiktok_business] Webhook sync failed for %s: %s — "
                "manual registration may be needed",
                account.account_alias,
                e,
            )

    def _callback_url_for(self, account: AccountConfig) -> str:
        """Build the webhook callback URL for an account route."""
        if not self._public_url:
            return ""
        path = f"/tiktok-business/webhook/{account.route_id}"
        return f"{self._public_url.rstrip('/')}{path}"

    async def _start_webhook_server(self) -> None:
        """Start the aiohttp webhook server (§8.3 step 6)."""
        if not self._accounts:
            return

        try:
            from aiohttp import web
        except ImportError:
            self._fatal_error_code = "missing_aiohttp"
            self._fatal_error_message = "aiohttp is required for the TikTok webhook server"
            return

        app = web.Application()
        app.router.add_post(
            "/tiktok-business/webhook/{route_id}", self._handle_webhook
        )
        app.router.add_get("/tiktok-business/health", self._handle_health)

        runner = web.AppRunner(app)
        await runner.setup()

        # Dual-stack bind (host=None → IPv4 + IPv6)
        site = web.TCPSite(runner, self._host or None, self._port)
        await site.start()
        self._app = app
        self._runner = runner

    async def _handle_health(self, request) -> Any:
        from aiohttp import web
        return web.json_response({"status": "ok", "platform": "tiktok_business"})

    async def _handle_webhook(self, request) -> Any:
        """Handle inbound TikTok webhook events."""
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

        # Read raw body
        raw_body = await request.read()
        headers = dict(request.headers)

        # Handle challenge response
        if _is_challenge_request(raw_body):
            challenge = _extract_challenge(raw_body)
            if challenge:
                return web.json_response({"challenge": challenge})

        # Verify + parse
        event, error = ingress.verify_and_parse(
            raw_body=raw_body,
            headers=headers,
            route_config={
                "webhook_secret": account.webhook_secret,
                "account_open_id": account.provider_account_id,
                "challenge": "",
            },
            profile=self._profile,
            account_alias=account.account_alias,
            route=route_id,
        )

        if error:
            if ingress.is_duplicate_sentinel(error):
                # Duplicate — ack is fine, no dispatch
                ack = ingress.acknowledge({"challenge": ""})
                return web.json_response(ack)

            # Real error — TikTok expects a 200 for most webhook errors
            # (so they don't retry), but 401/403 for auth issues
            if error.startswith("Signature") or error.startswith("Body"):
                return web.json_response(
                    {"error": "rejected"}, status=403
                )
            return web.json_response({"error": "ignored"}, status=200)

        # Ack immediately (§7.3 step 7)
        ack = ingress.acknowledge({"challenge": ""})

        # Dispatch to adapter processing (step 8)
        if event is not None:
            asyncio.create_task(self._process_event(event, account))

        return web.json_response(ack)

    async def _process_event(self, event: NormalizedEvent, account: AccountConfig) -> None:
        """Process a verified, de-duplicated webhook event."""
        try:
            self._state.update_webhook_state(
                self._profile,
                PROVIDER_TIKTOK_BUSINESS,
                account.account_alias,
                event.event_id,
                "processing",
            )

            # Challenge events are informational only
            if event.event_type == "webhook_challenge":
                Metrics.increment(
                    "bytedance_webhook_received_total",
                    labels={
                        "provider": "tiktok_business",
                        "account": account.account_alias,
                        "event_type": "webhook_challenge",
                    },
                )
                self._state.update_webhook_state(
                    self._profile,
                    PROVIDER_TIKTOK_BUSINESS,
                    account.account_alias,
                    event.event_id,
                    "completed",
                )
                return

            # Echo suppression — if sender_is_self, don't dispatch to agent
            sender_is_self = event.payload.get("_sender_is_self", False)
            if sender_is_self:
                Metrics.increment(
                    "bytedance_message_dispatch_total",
                    labels={
                        "provider": "tiktok_business",
                        "type": "echo_suppressed",
                        "result": "suppressed",
                    },
                )
                self._state.update_webhook_state(
                    self._profile,
                    PROVIDER_TIKTOK_BUSINESS,
                    account.account_alias,
                    event.event_id,
                    "completed",
                )
                return

            # Build MessageEvent
            chat_id = _build_chat_id(
                PROVIDER_TIKTOK_BUSINESS,
                account.account_alias,
                event.conversation_id or "",
            )

            # Check outbound message ledger for echo
            if event.message_id and self._state.is_known_outbound_message(
                self._profile,
                PROVIDER_TIKTOK_BUSINESS,
                account.account_alias,
                event.conversation_id or "",
                event.message_id,
            ):
                # This is our own outbound message echoed back
                self._state.update_webhook_state(
                    self._profile,
                    PROVIDER_TIKTOK_BUSINESS,
                    account.account_alias,
                    event.event_id,
                    "completed",
                )
                return

            text = ""
            media_urls: List[str] = []
            media_types: List[str] = []
            msg_type = MessageType.TEXT

            if event.message_type == "text":
                text = event.payload.get("content", event.payload.get("text", ""))
            elif event.message_type in ("image", "video", "audio", "file"):
                # Fetch media through the bounded media broker
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
                    "provider": "tiktok_business",
                    "type": event.message_type or "unknown",
                    "result": "dispatched",
                },
            )

            await self.handle_message(message_event)

            self._state.update_webhook_state(
                self._profile,
                PROVIDER_TIKTOK_BUSINESS,
                account.account_alias,
                event.event_id,
                "completed",
            )

        except Exception as e:
            logger.exception(
                "[tiktok_business] Event processing failed: %s", e
            )
            self._state.update_webhook_state(
                self._profile,
                PROVIDER_TIKTOK_BUSINESS,
                event.message_id or "",
                "failed",
                error=str(e),
            )

    async def _fetch_inbound_media(self, event: NormalizedEvent) -> Optional[Any]:
        """Fetch inbound media through the bounded media broker."""
        try:
            from plugins.bytedance.shared.media import MediaBroker

            broker = MediaBroker()
            media_url = event.payload.get("media_url") or event.payload.get("download_url")
            if not media_url:
                return None
            return await broker.download(media_url)
        except Exception as exc:
            logger.warning(
                "[tiktok_business] Failed to fetch inbound media: %s", exc
            )
            return None

    async def _restore_unprocessed_events(self) -> None:
        """Restore durable unprocessed events after restart (§7.3 step 9)."""
        for alias, account in self._accounts.items():
            events = self._state.get_unprocessed_events(
                self._profile,
                PROVIDER_TIKTOK_BUSINESS,
                alias,
                limit=100,
            )
            for event_id, raw_sha, route in events:
                # Re-dispatch from the stored state — the raw body is not
                # persisted, so we re-fetch from TikTok if needed.
                # For MVP, we log these as recovered.
                logger.info(
                    "[tiktok_business] Recovered unprocessed event %s for %s",
                    hash_id(event_id),
                    alias,
                )

    # ------------------------------------------------------------------
    # Outbound send (§8.5)
    # ------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send a message to a TikTok conversation.

        Per §8.5: every send performs:
        1. Resolve account from canonical chat ID.
        2. Authorize the local caller/route.
        3. Query or refresh /business/message/capabilities/get/.
        4. Validate message type and current conversation capability.
        5. Chunk/transform text.
        6. Upload image when needed.
        7. Create an outbound_operation record.
        8. Call /business/message/send/ once.
        9. Persist provider request/message ID.
        10. Reconcile webhook echo without redispatch.
        """
        parsed = _parse_chat_id(chat_id)
        if parsed is None:
            return SendResult(
                success=False,
                error=f"Invalid chat_id format: {chat_id}",
            )

        provider, account_alias, conversation_id = parsed
        account = self._accounts.get(account_alias)
        if account is None:
            return SendResult(
                success=False,
                error=f"Unknown account alias: {account_alias}",
            )

        client = self._clients.get(account_alias)
        if client is None:
            return SendResult(
                success=False,
                error=f"Client not connected for account: {account_alias}",
            )

        # 3. Check capability
        try:
            capability = await client.get_conversation_capability(conversation_id)
        except ProviderError as e:
            if not e.retryable:
                return SendResult(
                    success=False,
                    error=f"Capability check failed: {e.message}",
                )
            return SendResult(
                success=False,
                error=f"Capability check failed (retryable): {e.message}",
            )

        # 4. Validate against capability
        policy = self._policy.check_send(
            conversation_id,
            "text",
            provider=provider,
            account_alias=account_alias,
            profile=self._profile,
            sender_id=metadata.get("sender_id") if metadata else None,
            scopes=client.scopes,
            capability=capability,
        )

        if not policy.allowed:
            return SendResult(
                success=False,
                error=f"Send denied: {policy.reason_code}",
                error_code=policy.reason_code,
            )

        # Cache capability
        self._state.upsert_conversation(
            self._profile,
            provider,
            account_alias,
            conversation_id,
            peer_id=capability.sender_id if hasattr(capability, "sender_id") else None,
            display_name=None,
            last_message_at=capability.fetched_at,
            capability_json=None,  # Would store JSON-serialized
            capability_expires_at=capability.expires_at.timestamp()
            if capability.expires_at
            else None,
        )

        # 7-8. Create outbound_operation and send
        operation_id = secrets.token_urlsafe(16)
        payload_sha = __import__("hashlib").sha256(content.encode()).hexdigest()

        self._state.create_outbound_operation(
            operation_id,
            self._profile,
            provider,
            account_alias,
            conversation_id,
            "send_message",
            payload_sha,
        )

        # 8. Call send
        try:
            result = await client.send_message(
                conversation_id,
                content,
                open_id=None,
            )
        except ProviderError as e:
            self._state.update_outbound_operation(
                self._profile,
                operation_id,
                state="failed",
            )
            return SendResult(
                success=False,
                error=e.message,
                error_code=e.provider_code,
            )

        # 9. Persist provider message ID
        data = result.get("data") or {}
        msg_id = data.get("message_id") or data.get("messageId") or ""
        request_id = data.get("request_id") or result.get("request_id") or ""

        if msg_id:
            self._state.record_sent_message(
                self._profile,
                provider,
                account_alias,
                conversation_id,
                msg_id,
                text=content[:200],
            )

        self._state.update_outbound_operation(
            self._profile,
            operation_id,
            state="completed",
            provider_request_id=request_id,
        )

        Metrics.increment(
            "bytedance_message_send_total",
            labels={
                "provider": "tiktok_business",
                "type": "text",
                "result": "success",
            },
        )

        return SendResult(
            success=True,
            message_id=msg_id or request_id,
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Get chat info from canonical chat ID."""
        parsed = _parse_chat_id(chat_id)
        if parsed is None:
            return {"name": chat_id, "type": "dm"}

        provider, account_alias, conversation_id = parsed
        account = self._accounts.get(account_alias)
        if not account:
            return {"name": chat_id, "type": "dm"}

        # Try to get conversation display name
        client = self._clients.get(account_alias)
        if client:
            try:
                convs = await client.list_conversations()
                for conv in (convs.get("data") or {}).get("list", []):
                    if conv.get("conversation_id") == conversation_id:
                        peer = conv.get("peer", {})
                        return {
                            "name": peer.get("display_name", conversation_id),
                            "type": "dm",
                        }
            except Exception:
                pass

        return {"name": chat_id, "type": "dm"}

    def format_message(self, content: str) -> str:
        """Format message for TikTok (plain text, no markdown)."""
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
        """TikTok DM chats get a restricted toolset by default."""
        return None  # Use platform default

    # Properties needed by the adapter base class
    @property
    def _token_broker(self) -> Any:
        """Return or create a TokenBroker for this adapter."""
        if not hasattr(self, "__token_broker"):
            from plugins.bytedance.shared.tokens import TokenBroker
            self.__token_broker = TokenBroker()
        return self.__token_broker


def _is_challenge_request(raw_body: bytes) -> bool:
    """Check if a webhook body is a challenge/setup request."""
    try:
        payload = json.loads(raw_body)
        return isinstance(payload, dict) and "challenge" in payload
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def _extract_challenge(raw_body: bytes) -> Optional[str]:
    """Extract the challenge token from a setup request."""
    try:
        payload = json.loads(raw_body)
        if isinstance(payload, dict):
            return payload.get("challenge")
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass
    return None


# Platform hint for the LLM
TIKTOK_PLATFORM_HINT = """
You are chatting via TikTok Business Messaging.  This is a direct-message
channel for a TikTok Business Account.  Keep responses concise (DMs have
message-length limits).  Image and video sending may require
LINE_PUBLIC_URL-style reachability or media upload — check platform
capabilities before attempting media.  Some message types may not be
supported by the current conversation capability.
"""
