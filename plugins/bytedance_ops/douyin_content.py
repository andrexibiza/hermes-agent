"""Douyin content operations tools.

Per design spec §11.5: Douyin group-lane operations (content list,
account info, scope inspection, send-grant inspection).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from plugins.platforms.douyin.client import DouyinClient
from plugins.platforms.douyin.models import DouyinAccountConfig
from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.tokens import TokenBroker

logger = logging.getLogger(__name__)


class DouyinOps:
    """Douyin content and IM operations tools."""

    def __init__(
        self,
        *,
        profile: str = "default",
        accounts: Optional[Dict[str, DouyinAccountConfig]] = None,
        token_broker: Optional[TokenBroker] = None,
    ) -> None:
        self._profile = profile
        self._accounts = accounts or {}
        self._token_broker = token_broker or TokenBroker()

    def register_account(self, alias: str, account: DouyinAccountConfig) -> None:
        self._accounts[alias] = account

    def _get_client(self, account_alias: str) -> DouyinClient:
        account = self._accounts[account_alias]
        return DouyinClient(account, token_broker=self._token_broker)

    async def account_get(self, account_alias: str) -> Dict[str, Any]:
        """Read Douyin account info and scopes."""
        client = self._get_client(account_alias)
        try:
            scopes = await client.inspect_scopes()
            return {
                "account_alias": account_alias,
                "open_id": client.account.open_id,
                "scopes": sorted(scopes),
                "eligible": client.check_account_eligibility(),
                "region": client.account.region,
            }
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def scopes_get(self, account_alias: str) -> Dict[str, Any]:
        """Inspect granted scopes."""
        client = self._get_client(account_alias)
        try:
            scopes = await client.inspect_scopes()
            return {"account_alias": account_alias, "scopes": sorted(scopes)}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def content_list(
        self,
        account_alias: str,
        *,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List authorized Douyin content."""
        client = self._get_client(account_alias)
        try:
            result = await client.list_content(cursor=cursor)
            return result
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def message_capability_get(
        self,
        account_alias: str,
        conversation_short_id: str,
    ) -> Dict[str, Any]:
        """Inspect send-grant eligibility for a conversation.

        Per §11.4: this is a read-only inspection of the local grant
        state.  It does NOT consume the grant.
        """
        client = self._get_client(account_alias)
        try:
            grant = client.get_send_grant(conversation_short_id)
            return {
                "account_alias": account_alias,
                "conversation_short_id": conversation_short_id,
                "grant": grant.__dict__ if grant else None,
                "eligible": grant.eligible if grant else False,
            }
        finally:
            await client.close()
