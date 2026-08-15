"""Douyin Open Platform API client.

Per the design spec §3.3: the correct Douyin token type is selected by
the target app/account relationship.  Business tokens, user access
tokens, and client tokens are modeled as distinct credential classes
and are not interchangeable.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.http import BoundedApiClient, EndpointConfig
from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.tokens import AccountRef, TokenBroker
from plugins.platforms.douyin.models import (
    DouyinAPI,
    DouyinAccountConfig,
    DouyinSendGrant,
    REQUIRED_IM_SCOPES,
    SCOPE_IM_DIRECT_MESSAGE,
)

logger = logging.getLogger(__name__)


class DouyinTokenType:
    """Distinct credential classes for Douyin (not interchangeable).

    Per design spec §11.6: the correct token type is selected by the
    target app/account relationship.  Business tokens, user access
    tokens, and client tokens are not interchangeable and must be
    modeled as distinct credential classes.
    """

    USER_ACCESS_TOKEN = "user_access_token"
    CLIENT_TOKEN = "client_token"
    BUSINESS_TOKEN = "business_token"


class DouyinClient:
    """Douyin Open Platform API client.

    Supports:
    - User access tokens (for IM and content operations)
    - Client tokens (for app-level operations)
    - Scene-aware send grants
    - Account eligibility checks
    """

    def __init__(
        self,
        account: DouyinAccountConfig,
        *,
        token_broker: Optional[TokenBroker] = None,
    ) -> None:
        self.account = account
        self._token_broker = token_broker or TokenBroker()
        self.base_url = DouyinAPI.IM_BASE
        self._http = BoundedApiClient(
            self.base_url,
            default_headers={"Content-Type": "application/json"},
            default_endpoint="default",
        )
        self._http.register_endpoint(
            "default",
            EndpointConfig(max_retries=1, idempotent=True),
        )
        self._http.register_endpoint(
            "send",
            EndpointConfig(max_retries=2, idempotent=False),  # POST, not blindly retried
        )
        self._http.register_endpoint(
            "token",
            EndpointConfig(max_retries=1, idempotent=True),
        )

        self._scopes: Optional[set[str]] = None
        self._send_grants: Dict[str, DouyinSendGrant] = {}  # conversation_short_id -> grant

    async def close(self) -> None:
        await self._http.close()

    async def __aenter__(self) -> "DouyinClient":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def _get_account_ref(self) -> AccountRef:
        return AccountRef(
            provider=self.account.provider,
            profile=self.account.profile,
            account_alias=self.account.account_alias,
            provider_account_id=self.account.open_id,
            region=self.account.region,
        )

    async def _get_user_access_token(self) -> str:
        """Resolve the user access token for this account.

        Uses the profile-scoped secret store — never falls back to
        another profile's token.
        """
        from plugins.bytedance.shared.tokens import TokenBroker
        broker = self._token_broker
        info = await broker.acquire(
            self._get_account_ref(),
            access_token_secret=self.account.access_token_secret,
        )
        return info.access_token

    async def _get_client_token(self) -> str:
        """Resolve the client token for app-level operations."""
        from plugins.bytedance.shared.tokens import TokenBroker
        broker = self._token_broker
        info = await broker.acquire(
            self._get_account_ref(),
            access_token_secret="douyin_client_token",
        )
        return info.access_token

    async def inspect_scopes(self) -> set[str]:
        """Inspect granted scopes via the user info endpoint.

        Per §BD-16 acceptance: im.direct_message and account
        eligibility are checked.
        """
        token = await self._get_user_access_token()
        try:
            result = await self._http.request(
                "GET",
                DouyinAPI.USER_INFO,
                endpoint="default",
                headers={"access_token": token},
                params={
                    "open_id": self.account.open_id,
                    "access_token": token,
                },
            )
        except ProviderError:
            return set()

        data = result.get("data") or {}
        # Douyin may return scopes in various fields
        raw_scopes = data.get("scopes") or data.get("scope") or ""
        if isinstance(raw_scopes, list):
            scope_set = set(raw_scopes)
        elif isinstance(raw_scopes, str):
            scope_set = set(raw_scopes.split())
        else:
            scope_set = set()

        self._scopes = scope_set
        return scope_set

    @property
    def scopes(self) -> set[str]:
        return self._scopes or set()

    def check_account_eligibility(self) -> bool:
        """Check if the account is eligible for IM operations.

        Per §BD-16: im.direct_message scope and account eligibility
        are material gates.
        """
        if not self._scopes:
            return False
        return SCOPE_IM_DIRECT_MESSAGE in self._scopes

    # ------------------------------------------------------------------
    # IM messaging endpoints
    # ------------------------------------------------------------------

    async def send_private_msg(
        self,
        to_user_id: str,
        content: str,
        *,
        msg_type: str = "text",
        open_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Send a private message via /im/send_private_msg/.

        Per §11.4: scene-aware — the caller must hold a valid grant
        before calling this method.
        """
        token = await self._get_user_access_token()
        body: Dict[str, Any] = {
            "open_id": open_id or self.account.open_id,
            "access_token": token,
            "to_user_id": to_user_id,
            "msg_type": msg_type,
            "content": content,
        }
        return await self._http.request(
            "POST",
            DouyinAPI.SEND_PRIVATE_MSG,
            endpoint="send",
            headers={"access_token": token},
            json_body=body,
            idempotency_key=f"dy_send_{to_user_id}_{int(time.time())}",
        )

    async def receive_msg(
        self, *, cursor: Optional[str] = None, limit: int = 50
    ) -> Dict[str, Any]:
        """Receive messages via /im/receive_msg/ (polling)."""
        token = await self._get_user_access_token()
        params: Dict[str, Any] = {
            "open_id": self.account.open_id,
            "access_token": token,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "POST",
            DouyinAPI.RECEIVE_MSG,
            endpoint="default",
            headers={"access_token": token},
            json_body=params,
        )

    async def enter_direct_msg(
        self, conversation_short_id: str
    ) -> Dict[str, Any]:
        """Send the entry-to-conversation event.

        Per §11.2: creates a short-lived send-capability snapshot.
        """
        token = await self._get_user_access_token()
        body = {
            "open_id": self.account.open_id,
            "access_token": token,
            "conversation_short_id": conversation_short_id,
        }
        return await self._http.request(
            "POST",
            DouyinAPI.ENTER_DIRECT_MSG,
            endpoint="default",
            headers={"access_token": token},
            json_body=body,
        )

    async def recall_msg(self, server_message_id: str) -> Dict[str, Any]:
        """Recall a message, correlating by server_message_id (§11.2)."""
        token = await self._get_user_access_token()
        body = {
            "open_id": self.account.open_id,
            "access_token": token,
            "server_message_id": server_message_id,
        }
        return await self._http.request(
            "POST",
            DouyinAPI.RECALL_MSG,
            endpoint="default",
            headers={"access_token": token},
            json_body=body,
        )

    # ------------------------------------------------------------------
    # Send grant management (§11.4)
    # ------------------------------------------------------------------

    def create_send_grant(
        self,
        conversation_short_id: str,
        scene: str,
        *,
        expires_at: Optional[float] = None,
        remaining_count: Optional[int] = 1,
        source_event_id: Optional[str] = None,
        eligible: bool = True,
        reason: Optional[str] = None,
    ) -> DouyinSendGrant:
        """Create a scene-aware send grant.

        Called when im_enter_direct_msg or im_receive_msg events are
        received — the adapter translates the provider event into a
        grant and stores it.
        """
        from datetime import datetime, timezone

        grant = DouyinSendGrant(
            scene=scene,
            source_event_id=source_event_id,
            conversation_short_id=conversation_short_id,
            reply_message_id=None,
            expires_at=datetime.fromtimestamp(expires_at, tz=timezone.utc) if expires_at else None,
            remaining_count=remaining_count,
            eligible=eligible,
            reason=reason,
        )
        self._send_grants[conversation_short_id] = grant
        return grant

    def consume_send_grant(self, conversation_short_id: str) -> bool:
        """Atomically consume a send-grant allowance.

        Returns True if the grant was valid and consumed.
        Per §11.4: send must select a valid grant and atomically
        consume its local allowance.
        """
        grant = self._send_grants.get(conversation_short_id)
        if grant is None:
            return False
        if not grant.eligible:
            return False
        if grant.expires_at and time.time() >= grant.expires_at.timestamp():
            return False
        if grant.remaining_count is not None and grant.remaining_count <= 0:
            return False

        # Atomically consume
        if grant.remaining_count is not None:
            new_count = grant.remaining_count - 1
            self._send_grants[conversation_short_id] = DouyinSendGrant(
                scene=grant.scene,
                source_event_id=grant.source_event_id,
                conversation_short_id=grant.conversation_short_id,
                reply_message_id=grant.reply_message_id,
                expires_at=grant.expires_at,
                remaining_count=new_count,
                eligible=grant.eligible,
                reason=grant.reason,
            )
        return True

    def get_send_grant(self, conversation_short_id: str) -> Optional[DouyinSendGrant]:
        return self._send_grants.get(conversation_short_id)

    def revoke_grant(self, conversation_short_id: str) -> None:
        """Revoke a send grant (e.g. on contract_unauthorize)."""
        self._send_grants.pop(conversation_short_id, None)

    # ------------------------------------------------------------------
    # Account contract events (§11.2)
    # ------------------------------------------------------------------

    def handle_authorize(self) -> None:
        """Refresh account/scope state on contract_authorize."""
        self._scopes = None  # Force re-inspect
        # Clear grants — they need re-validation
        self._send_grants.clear()

    def handle_unauthorize(self) -> None:
        """Immediately disable the affected account on contract_unauthorize."""
        self._send_grants.clear()
        logger.warning(
            "Douyin account %s unauthorized — all grants revoked",
            self.account.account_alias,
        )

    # ------------------------------------------------------------------
    # Content operations
    # ------------------------------------------------------------------

    async def list_content(self, *, cursor: Optional[str] = None) -> Dict[str, Any]:
        """List authorized content."""
        token = await self._get_user_access_token()
        params = {
            "open_id": self.account.open_id,
            "access_token": token,
        }
        if cursor:
            params["cursor"] = cursor
        return await self._http.request(
            "GET",
            DouyinAPI.VIDEO_LIST,
            endpoint="default",
            headers={"access_token": token},
            params=params,
        )


import time  # noqa: E402
