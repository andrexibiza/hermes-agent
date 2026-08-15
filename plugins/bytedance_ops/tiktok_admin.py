"""TikTok Business Messaging admin tools (BD-11).

Per design spec §4.3–§4.6 and §8: admin tools for managing
auto-messages, conversation folders, message status, webhook config,
and directory operations.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.state import StateStore, get_state_store
from plugins.platforms.tiktok_business.client import TikTokBusinessClient
from plugins.platforms.tiktok_business.models import (
    AccountConfig,
    AutoMessageCreatePayload,
    AutoMessageStatus,
    PROVIDER_TIKTOK_BUSINESS,
)

logger = logging.getLogger(__name__)


class TikTokBusinessAdmin:
    """Admin tools for TikTok Business Messaging.

    Requires ``admin_message`` and ``admin_conversation`` scopes.
    """

    def __init__(
        self,
        *,
        profile: str = "default",
        accounts: Optional[Dict[str, AccountConfig]] = None,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self._profile = profile
        self._accounts = accounts or {}
        self._state = state_store or get_state_store()

    def register_account(self, alias: str, account: AccountConfig) -> None:
        self._accounts[alias] = account

    def _get_client(self, account_alias: str) -> TikTokBusinessClient:
        account = self._accounts[account_alias]
        return TikTokBusinessClient(account)

    # ------------------------------------------------------------------
    # Auto messages (§4.4)
    # ------------------------------------------------------------------

    async def auto_messages_list(
        self, account_alias: str
    ) -> Dict[str, Any]:
        """List configured auto-messages."""
        client = self._get_client(account_alias)
        try:
            result = await client.list_auto_messages()
            data = result.get("data") or []
            messages = []
            for msg in data:
                messages.append({
                    "id": msg.get("id"),
                    "name": msg.get("name"),
                    "content": msg.get("content"),
                    "status": msg.get("status"),
                    "create_time": msg.get("create_time"),
                    "update_time": msg.get("update_time"),
                })
            return {"auto_messages": messages}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def auto_message_create(
        self,
        account_alias: str,
        name: str,
        content: str,
        *,
        status: str = "ACTIVE",
        priority: int = 0,
    ) -> Dict[str, Any]:
        """Create a new auto-message."""
        client = self._get_client(account_alias)
        try:
            payload = AutoMessageCreatePayload(
                name=name,
                content=content,
                status=AutoMessageStatus(status),
                priority=priority,
            )
            result = await client.create_auto_message(payload)
            data = result.get("data") or {}
            Metrics.increment("bytedance_admin_action", labels={
                "provider": "tiktok_business", "action": "auto_message_create",
            })
            return {"success": True, "auto_message_id": data.get("message_id")}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def auto_message_update(
        self,
        account_alias: str,
        message_id: str,
        *,
        content: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Update an existing auto-message."""
        client = self._get_client(account_alias)
        try:
            update_fields: Dict[str, Any] = {}
            if content is not None:
                update_fields["content"] = content
            if status is not None:
                update_fields["status"] = status
            if priority is not None:
                update_fields["priority"] = priority

            result = await client.update_auto_message(message_id, update_fields)
            Metrics.increment("bytedance_admin_action", labels={
                "provider": "tiktok_business", "action": "auto_message_update",
            })
            return {"success": True, "id": message_id}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def auto_message_status_update(
        self,
        account_alias: str,
        message_id: str,
        status: str,
    ) -> Dict[str, Any]:
        """Update only the status of an auto-message."""
        client = self._get_client(account_alias)
        try:
            result = await client.update_auto_message_status(message_id, status)
            Metrics.increment("bytedance_admin_action", labels={
                "provider": "tiktok_business", "action": "auto_message_status",
            })
            return {"success": True, "id": message_id, "status": status}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def auto_message_sort(
        self, account_alias: str, ordered_ids: List[str]
    ) -> Dict[str, Any]:
        """Reorder auto-messages by priority."""
        client = self._get_client(account_alias)
        try:
            result = await client.sort_auto_messages(ordered_ids)
            Metrics.increment("bytedance_admin_action", labels={
                "provider": "tiktok_business", "action": "auto_message_sort",
            })
            return {"success": True}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def auto_message_delete(
        self, account_alias: str, message_id: str
    ) -> Dict[str, Any]:
        """Delete an auto-message."""
        client = self._get_client(account_alias)
        try:
            result = await client.delete_auto_message(message_id)
            Metrics.increment("bytedance_admin_action", labels={
                "provider": "tiktok_business", "action": "auto_message_delete",
            })
            return {"success": True, "id": message_id}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Webhook config (§4.6)
    # ------------------------------------------------------------------

    async def webhook_config_get(
        self, account_alias: str
    ) -> Dict[str, Any]:
        """Get webhook configuration for an account."""
        client = self._get_client(account_alias)
        try:
            result = await client.get_webhook_config()
            return result.get("data") or {}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def webhook_config_update(
        self,
        account_alias: str,
        *,
        webhook_url: Optional[str] = None,
        secret: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Update webhook configuration."""
        client = self._get_client(account_alias)
        try:
            update_fields: Dict[str, Any] = {}
            if webhook_url is not None:
                update_fields["webhook_url"] = webhook_url
            if secret is not None:
                update_fields["secret"] = secret
            if enabled is not None:
                update_fields["status"] = "OPEN" if enabled else "CLOSE"

            result = await client.update_webhook_config(update_fields)
            Metrics.increment("bytedance_admin_action", labels={
                "provider": "tiktok_business", "action": "webhook_update",
            })
            return {"success": True}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def webhook_config_register(
        self, account_alias: str, webhook_url: str, secret: str
    ) -> Dict[str, Any]:
        """Register a webhook URL for an account."""
        client = self._get_client(account_alias)
        try:
            result = await client.register_webhook(webhook_url, secret)
            Metrics.increment("bytedance_admin_action", labels={
                "provider": "tiktok_business", "action": "webhook_register",
            })
            return {"success": True}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Conversation folders (§4.5)
    # ------------------------------------------------------------------

    async def folders_list(
        self, account_alias: str
    ) -> Dict[str, Any]:
        """List conversation folders."""
        client = self._get_client(account_alias)
        try:
            result = await client.list_conversation_folders()
            data = result.get("data") or []
            folders = []
            for f in data:
                folders.append({
                    "id": f.get("id"),
                    "name": f.get("name"),
                    "conversation_count": f.get("conversation_count", 0),
                    "is_default": f.get("is_default", False),
                })
            return {"folders": folders}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def folder_conversations(
        self,
        account_alias: str,
        folder_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """List conversations in a folder."""
        client = self._get_client(account_alias)
        try:
            result = await client.list_folder_conversations(
                folder_id, cursor=cursor, limit=limit,
            )
            return result.get("data") or {}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Message status / history (§4.7)
    # ------------------------------------------------------------------

    async def conversation_messages(
        self,
        account_alias: str,
        conversation_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 20,
    ) -> Dict[str, Any]:
        """Read message history for a conversation.

        Uses local state store (from webhook receipts) as primary
        source, falling back to provider API for historical data.
        """
        # Try local state first
        local_records = self._state.get_conversation_messages(
            self._profile,
            PROVIDER_TIKTOK_BUSINESS,
            account_alias,
            conversation_id,
            limit=limit,
        )
        if local_records:
            messages = []
            for rec in local_records:
                messages.append({
                    "message_id": rec.get("message_id"),
                    "text": rec.get("text"),
                    "direction": rec.get("direction"),
                    "created_at": rec.get("created_at"),
                    "status": rec.get("status"),
                })
            return {"messages": messages, "source": "local"}

        # Fallback to provider
        client = self._get_client(account_alias)
        try:
            result = await client.get_conversation_messages(
                conversation_id, cursor=cursor, limit=limit,
            )
            return result.get("data") or {}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def message_status(
        self,
        account_alias: str,
        conversation_id: str,
        message_id: str,
    ) -> Dict[str, Any]:
        """Get delivery/read status of a message."""
        client = self._get_client(account_alias)
        try:
            result = await client.get_message_status(
                conversation_id, message_id,
            )
            return result.get("data") or {}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()
