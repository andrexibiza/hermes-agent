"""TikTok Business API client with scope inspection.

Per the design spec §7.7 (BD-07 acceptance):
- v1.3 base/endpoint handling
- token inspector
- read/send/auto-message feature gates
- account identity binding
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.http import BoundedApiClient, EndpointConfig
from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.tokens import AccountRef, TokenBroker
from plugins.platforms.tiktok_business.models import (
    ACCOUNT_WEBHOOK_CONFIG,
    ACCOUNT_IDENTITY,
    AUTO_MESSAGE_CREATE,
    AUTO_MESSAGE_DELETE,
    AUTO_MESSAGE_LIST,
    AUTO_MESSAGE_SORT,
    AUTO_MESSAGE_STATUS as AUTO_MESSAGE_STATUS_UPDATE,
    AUTO_MESSAGE_UPDATE,
    AutoMessageStatus,
    BUSINESS_GET,
    CTM_GET,
    CTM_UPDATE,
    COMMENT_CREATE,
    COMMENT_DELETE,
    COMMENT_HIDE,
    COMMENT_IMAGE_UPLOAD,
    COMMENT_LIKE,
    COMMENT_LIST,
    COMMENT_REPLY_CREATE,
    COMMENT_REPLY_LIST,
    CONVERSATION_GET,
    CONVERSATION_LIST,
    CREATOR_AUTH_URL,
    CREATOR_POST,
    DOWNLOAD_MEDIA,
    FOLDER_CONVERSATIONS,
    FOLDER_LIST,
    GET_CAPABILITIES,
    MESSAGE_LIST,
    MESSAGE_STATUS,
    TOKEN_INFO,
    UPLOAD_MEDIA,
    VIDEO_LIST,
    VIDEO_PUBLISH,
    VIDEO_SETTINGS,
    PHOTO_PUBLISH,
    PUBLISH_STATUS,
    WEBHOOK_LIST as WEBHOOK_LIST_ENDPOINT,
    WEBHOOK_UPDATE as WEBHOOK_UPDATE_ENDPOINT,
    TikTokBusinessAPI,
    TikTokMessage,
    TikTokConversation,
    AccountConfig,
    ConversationCapability,
    capabilities_from_scopes,
    scope_set_from_token_info,
)

logger = logging.getLogger(__name__)


class TikTokBusinessClient:
    """TikTok Business API client with scoped feature activation.

    Feature gates are determined by inspecting actual granted scopes
    at connect time (design spec §8.3: verify required scopes).
    """

    def __init__(
        self,
        account: AccountConfig,
        *,
        token_broker: Optional[TokenBroker] = None,
    ) -> None:
        self.account = account
        self._token_broker = token_broker or TokenBroker()
        self._http = BoundedApiClient(
            TikTokBusinessAPI.BASE_URL + TikTokBusinessAPI.VERSION,
            default_headers={"Content-Type": "application/json"},
            default_endpoint="default",
        )
        # Register endpoint configs
        self._http.register_endpoint(
            "token_inspect",
            EndpointConfig(max_retries=1, timeout=_tt(10)),
        )
        self._http.register_endpoint(
            "send",
            EndpointConfig(max_retries=2, idempotent=True, timeout=_tt(30)),
        )
        self._http.register_endpoint(
            "list",
            EndpointConfig(max_retries=1, idempotent=True, timeout=_tt(30)),
        )
        self._http.register_endpoint(
            "capabilities",
            EndpointConfig(max_retries=1, idempotent=True, timeout=_tt(15)),
        )
        self._http.register_endpoint(
            "default",
            EndpointConfig(max_retries=1, idempotent=True, timeout=_tt(20)),
        )
        self._http.register_endpoint(
            "token_refresh",
            EndpointConfig(max_retries=0, idempotent=True, timeout=_tt(15)),
        )

        # Feature gates (populated by inspect_scopes)
        self._scopes: Optional[set[str]] = None
        self._features: Dict[str, bool] = {}

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "TikTokBusinessClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Token inspection + scope discovery (§8.3 step 2)
    # ------------------------------------------------------------------

    async def inspect_token(self) -> Dict[str, Any]:
        """Call /tt_user/token_info/get/ to inspect the access token.

        Returns the raw provider response, which includes granted scopes.
        """
        token = await self._get_token()
        result = await self._http.request(
            "GET",
            TOKEN_INFO,
            endpoint="token_inspect",
            headers={"Access-Token": token.access_token},
            params={"access_token": token.access_token},
        )
        return result

    def _get_account_ref(self) -> AccountRef:
        return AccountRef(
            provider=self.account.provider,
            profile=self.account.profile,
            account_alias=self.account.account_alias,
            provider_account_id=self.account.provider_account_id,
            region=self.account.region,
        )

    async def _get_token(self):
        """Resolve the access token for this account."""
        if not self.account.access_token_secret:
            raise ProviderError(
                f"No access_token_secret configured for {self.account.account_alias}",
                retryable=False,
            )
        return await self._token_broker.acquire(
            self._get_account_ref(),
            access_token_secret=self.account.access_token_secret,
        )

    async def inspect_scopes(self) -> set[str]:
        """Inspect granted scopes and activate matching features.

        Returns the set of granted scope strings.  Caches the result
        in ``self._features``.
        """
        try:
            info = await self.inspect_token()
            data = info.get("data") or info.get("result") or {}
            scope_set = scope_set_from_token_info(data)
        except ProviderError as e:
            logger.warning(
                "TikTok token inspection failed for %s: %s",
                self.account.account_alias,
                e,
            )
            scope_set = frozenset()

        self._scopes = scope_set
        self._features = capabilities_from_scopes(scope_set)

        Metrics.increment(
            "bytedance_token_refresh_total",
            labels={
                "provider": "tiktok_business",
                "result": "success" if scope_set else "failure",
            },
        )

        return scope_set

    @property
    def scopes(self) -> set[str]:
        """Return the granted scopes (empty if not yet inspected)."""
        return self._scopes or set()

    @property
    def features(self) -> Dict[str, bool]:
        """Return the feature-capability map."""
        return self._features

    def has_feature(self, feature: str) -> bool:
        """Check if a feature is available based on granted scopes."""
        return self._features.get(feature, False)

    # ------------------------------------------------------------------
    # Messaging endpoints (§8.3 / §8.5)
    # ------------------------------------------------------------------

    async def send_message(
        self,
        conversation_id: str,
        text: Optional[str] = None,
        *,
        image_url: Optional[str] = None,
        open_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a message via /business/message/send/.

        Validates capability before sending (the adapter layer checks
        ConversationCapability; this client enforces the API call).
        """
        if not self.has_feature("send"):
            raise ProviderError(
                f"Account {self.account.account_alias} lacks business_messaging_send scope",
                retryable=False,
                context={"required_scope": "business_messaging_send"},
            )
        token = await self._get_token()
        body: Dict[str, Any] = {
            "conversation_id": conversation_id,
        }
        if text is not None:
            body["text"] = text
        if image_url is not None:
            body["image_url"] = image_url
        if open_id is not None:
            body["open_id"] = open_id

        result = await self._http.request(
            "POST",
            "/v1.3/message/send/",
            endpoint="send",
            headers={"Access-Token": token.access_token},
            json_body=body,
        )
        return result

    async def get_conversation_capability(
        self, conversation_id: str
    ) -> ConversationCapability:
        """Query /business/message/capabilities/get/ for conversation capability."""
        token = await self._get_token()
        result = await self._http.request(
            "GET",
            GET_CAPABILITIES,
            endpoint="capabilities",
            headers={"Access-Token": token.access_token},
            params={"conversation_id": conversation_id},
        )

        data = result.get("data") or {}
        can_send = data.get("can_send", False)
        allowed_types_raw = data.get("allowed_message_types", [])
        max_remaining = data.get("max_messages_remaining")
        expires_at_str = data.get("expires_at")
        expires_at = _parse_dt(expires_at_str)

        return ConversationCapability(
            provider="tiktok_business",
            account_alias=self.account.account_alias,
            conversation_id=conversation_id,
            can_send=can_send,
            allowed_message_types=frozenset(allowed_types_raw),
            max_messages_remaining=max_remaining,
            expires_at=expires_at,
            source_event_id=data.get("source_event_id"),
            reason_code=data.get("reason_code"),
            fetched_at=None,  # Will be set by caller
        )

    async def list_conversations(
        self, *, cursor: Optional[str] = None, page_size: int = 50
    ) -> Dict[str, Any]:
        """List conversations via /business/message/conversation/list/."""
        token = await self._get_token()
        params: Dict[str, Any] = {"page_size": page_size}
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "GET",
            "/v1.3/message/conversation/list/",
            endpoint="list",
            headers={"Access-Token": token.access_token},
            params=params,
        )

    async def list_messages(
        self, conversation_id: str, *, cursor: Optional[str] = None, page_size: int = 50
    ) -> Dict[str, Any]:
        """List messages via /business/message/content/list/."""
        token = await self._get_token()
        params: Dict[str, Any] = {
            "conversation_id": conversation_id,
            "page_size": page_size,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "GET",
            "/v1.3/message/content/list/",
            endpoint="list",
            headers={"Access-Token": token.access_token},
            params=params,
        )

    async def upload_media(self, media_id: str, image_path: str) -> Dict[str, Any]:
        """Upload image media via /business/message/media/upload/."""
        token = await self._get_token()
        # TikTok accepts multipart or a URL — here we use the path
        # as file upload
        import os

        if not os.path.exists(image_path):
            raise ProviderError(f"Image file not found: {image_path}", retryable=False)

        # Use raw body (multipart) — aiohttp handles this in _do_request
        # but for simplicity here, we'd use a multipart form.  For the MVP,
        # we pass the path and let the adapter handle upload.
        from pathlib import Path
        file_bytes = await asyncio.to_thread(Path(image_path).read_bytes)
        return await self._http.request(
            "POST",
            UPLOAD_MEDIA,
            endpoint="default",
            headers={
                "Access-Token": token.access_token,
                "Content-Type": "application/octet-stream",
            },
            raw_body=file_bytes,
        )

    async def download_media(self, media_id: str) -> bytes:
        """Download media via /business/message/media/download/."""
        token = await self._get_token()
        result = await self._http.request(
            "GET",
            DOWNLOAD_MEDIA,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"media_id": media_id},
        )
        # Result is raw bytes (text) or base64-encoded in JSON
        if isinstance(result, str):
            # Raw bytes returned as text — decode
            return result.encode("utf-8")
        if isinstance(result, dict):
            import base64
            b64 = result.get("data", {}).get("content") or result.get("content")
            if b64:
                return base64.b64decode(b64)
        return b""

    # ------------------------------------------------------------------
    # Webhook management (§8.3 step 5)
    # ------------------------------------------------------------------

    async def configure_webhook(
        self,
        webhook_url: str,
        events: List[str],
        *,
        auto_send_read_receipt: bool = True,
    ) -> Dict[str, Any]:
        """Call /business/webhook/update/ to configure webhook delivery.

        Only when ``manage_webhook=true`` in account config.
        """
        token = await self._get_token()
        body: Dict[str, Any] = {
            "webhook_url": webhook_url,
            "events": events,
            "auto_send_read_receipt": auto_send_read_receipt,
            "business_account_id": self.account.provider_account_id,
        }
        return await self._http.request(
            "POST",
            WEBHOOK_UPDATE_ENDPOINT,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body=body,
        )

    async def list_webhooks(self) -> Dict[str, Any]:
        """Call /business/webhook/list/ to check current webhook config."""
        token = await self._get_token()
        return await self._http.request(
            "GET",
            WEBHOOK_LIST_ENDPOINT,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"business_account_id": self.account.provider_account_id},
        )

    # ------------------------------------------------------------------
    # Auto-message administration
    # ------------------------------------------------------------------

    async def create_auto_message(self, config: Dict[str, Any]) -> Dict[str, Any]:
        token = await self._get_token()
        # Handle dataclass payloads
        if hasattr(config, "__dict__") and not isinstance(config, dict):
            config = {k: v for k, v in config.__dict__.items() if v is not None}
            if isinstance(config.get("status"), AutoMessageStatus):
                config["status"] = config["status"].value
        return await self._http.request(
            "POST",
            AUTO_MESSAGE_CREATE,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body=config,
        )

    async def update_auto_message(self, config: Dict[str, Any]) -> Dict[str, Any]:
        token = await self._get_token()
        return await self._http.request(
            "POST",
            AUTO_MESSAGE_UPDATE,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body=config,
        )

    async def list_auto_messages(self) -> Dict[str, Any]:
        token = await self._get_token()
        return await self._http.request(
            "GET",
            AUTO_MESSAGE_LIST,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"business_account_id": self.account.provider_account_id},
        )

    async def update_auto_message_status(
        self, message_id: str, status: str
    ) -> Dict[str, Any]:
        """Update auto-message status (ENABLE/DISABLE or ACTIVE/INACTIVE)."""
        token = await self._get_token()
        # Normalize status
        status_upper = status.upper()
        if status_upper in ("ACTIVE", "ENABLE"):
            api_status = "ENABLE"
        elif status_upper in ("INACTIVE", "DISABLE"):
            api_status = "DISABLE"
        else:
            api_status = status_upper
        return await self._http.request(
            "POST",
            AUTO_MESSAGE_STATUS_UPDATE,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body={
                "message_id": message_id,
                "status": api_status,
            },
        )

    async def sort_auto_messages(self, ordered_ids: List[str]) -> Dict[str, Any]:
        """Reorder auto-messages by providing ordered IDs."""
        token = await self._get_token()
        return await self._http.request(
            "POST",
            AUTO_MESSAGE_SORT,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body={"sort_list": [{"id": mid} for mid in ordered_ids]},
        )

    async def delete_auto_message(self, message_id: str) -> Dict[str, Any]:
        """Delete an auto-message."""
        token = await self._get_token()
        return await self._http.request(
            "POST",
            AUTO_MESSAGE_DELETE,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body={"message_id": message_id},
        )

    # ------------------------------------------------------------------
    # Webhook config (§4.6)
    # ------------------------------------------------------------------

    async def get_webhook_config(self) -> Dict[str, Any]:
        """Get webhook endpoint config via account/webhook/config/."""
        token = await self._get_token()
        return await self._http.request(
            "GET",
            ACCOUNT_WEBHOOK_CONFIG,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"business_account_id": self.account.provider_account_id},
        )

    async def update_webhook_config(self, config: Dict[str, Any]) -> Dict[str, Any]:
        """Update webhook endpoint config."""
        token = await self._get_token()
        body = {"business_account_id": self.account.provider_account_id}
        body.update(config)
        return await self._http.request(
            "POST",
            ACCOUNT_WEBHOOK_CONFIG,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body=body,
        )

    async def register_webhook(self, webhook_url: str, secret: str) -> Dict[str, Any]:
        """Register a webhook URL (via webhook/update/ with status=OPEN)."""
        token = await self._get_token()
        return await self._http.request(
            "POST",
            WEBHOOK_UPDATE_ENDPOINT,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body={
                "business_account_id": self.account.provider_account_id,
                "webhook_url": webhook_url,
                "secret": secret,
                "status": "OPEN",
            },
        )

    # ------------------------------------------------------------------
    # Comment-to-Message
    # ------------------------------------------------------------------

    async def get_comment_to_message(self) -> Dict[str, Any]:
        token = await self._get_token()
        return await self._http.request(
            "GET",
            CTM_GET,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"business_account_id": self.account.provider_account_id},
        )

    async def update_comment_to_message(self, config: Dict[str, Any]) -> Dict[str, Any]:
        token = await self._get_token()
        return await self._http.request(
            "POST",
            CTM_UPDATE,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body=config,
        )

    # ------------------------------------------------------------------
    # Organic / Accounts API
    # ------------------------------------------------------------------

    async def get_business_account_info(self) -> Dict[str, Any]:
        """Get Business Account profile data via /business/get/."""
        token = await self._get_token()
        return await self._http.request(
            "GET",
            BUSINESS_GET,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"business_account_id": self.account.provider_account_id},
        )

    async def list_posts(
        self, *, cursor: Optional[str] = None, page_size: int = 20
    ) -> Dict[str, Any]:
        """List owned posts via /business/video/list/."""
        token = await self._get_token()
        params: Dict[str, Any] = {
            "business_account_id": self.account.provider_account_id,
            "page_size": page_size,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "GET",
            VIDEO_LIST,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params=params,
        )

    async def list_comments(
        self, video_id: str, *, cursor: Optional[str] = None, page_size: int = 50
    ) -> Dict[str, Any]:
        """List comments on a post via /business/comment/list/."""
        token = await self._get_token()
        params: Dict[str, Any] = {
            "video_id": video_id,
            "page_size": page_size,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "GET",
            COMMENT_LIST,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params=params,
        )

    async def publish_video(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Publish a video post via /business/video/publish/."""
        token = await self._get_token()
        return await self._http.request(
            "POST",
            VIDEO_PUBLISH,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body=payload,
        )

    async def publish_photo(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Publish a photo post via /business/photo/publish/."""
        token = await self._get_token()
        return await self._http.request(
            "POST",
            PHOTO_PUBLISH,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            json_body=payload,
        )

    async def get_publish_status(self, publish_id: str) -> Dict[str, Any]:
        """Check publish status via /business/publish/status/."""
        token = await self._get_token()
        return await self._http.request(
            "GET",
            PUBLISH_STATUS,
            endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"publish_id": publish_id},
        )

    async def check_account_identity(self) -> Dict[str, Any]:
        """Fetch Business Account identity to verify binding (§8.3 step 4).

        Returns the account info so the adapter can confirm the
        configured business_account_id matches the token's account.
        """
        return await self.get_business_account_info()

    # ------------------------------------------------------------------
    # Admin / conversation methods (§4.3–§4.7)
    # ------------------------------------------------------------------

    async def list_conversations(self, *, cursor: Optional[str] = None) -> Dict[str, Any]:
        """List conversations via /message/conversation/list/."""
        token = await self._get_token()
        params: Dict[str, Any] = {
            "open_id": self.account.open_id,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "GET", CONVERSATION_LIST, endpoint="default",
            headers={"Access-Token": token.access_token}, params=params,
        )

    async def get_conversation(self, conversation_id: str) -> Dict[str, Any]:
        """Get conversation details via /message/conversation/get/."""
        token = await self._get_token()
        return await self._http.request(
            "GET", CONVERSATION_GET, endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"conversation_id": conversation_id},
        )

    async def list_conversation_folders(self) -> Dict[str, Any]:
        """List conversation folders via /message/folder/list/."""
        token = await self._get_token()
        return await self._http.request(
            "GET", FOLDER_LIST, endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"open_id": self.account.open_id},
        )

    async def list_folder_conversations(
        self, folder_id: str, *, cursor: Optional[str] = None, limit: int = 20
    ) -> Dict[str, Any]:
        """List conversations in a folder."""
        token = await self._get_token()
        params: Dict[str, Any] = {
            "folder_id": folder_id,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "GET", FOLDER_CONVERSATIONS, endpoint="default",
            headers={"Access-Token": token.access_token}, params=params,
        )

    async def get_conversation_messages(
        self, conversation_id: str, *, cursor: Optional[str] = None, limit: int = 20
    ) -> Dict[str, Any]:
        """Get message history for a conversation via /message/list/."""
        token = await self._get_token()
        params: Dict[str, Any] = {
            "conversation_id": conversation_id,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "GET", MESSAGE_LIST, endpoint="default",
            headers={"Access-Token": token.access_token}, params=params,
        )

    async def get_message_status(
        self, conversation_id: str, message_id: str
    ) -> Dict[str, Any]:
        """Get message delivery/read status."""
        token = await self._get_token()
        return await self._http.request(
            "GET", MESSAGE_STATUS, endpoint="default",
            headers={"Access-Token": token.access_token},
            params={"conversation_id": conversation_id, "message_id": message_id},
        )

    async def get_creator_auth_url(
        self, redirect_uri: str, *, state: str = "", scope: str = "video.create"
    ) -> str:
        """Build TikTok Creator OAuth URL (§10.3)."""
        import urllib.parse
        params = {
            "client_key": self.account.client_key,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scope,
        }
        return CREATOR_AUTH_URL + urllib.parse.urlencode(params)


def _tt(seconds: float) -> Any:
    """Build an aiohttp timeout."""
    import aiohttp
    return aiohttp.ClientTimeout(total=seconds)


def _parse_dt(value: Optional[str]) -> Optional[Any]:
    """Parse an ISO datetime string, returning None on failure."""
    if not value:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
