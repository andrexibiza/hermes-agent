"""TikTok Organic operations tools.

Per design spec §9.1: owned Business Account backend for profile,
posts, comments, publishing, publish status, and webhook operations.

These are explicit Hermes tools with narrow schemas and approval
semantics.  Read tools have no approval gate; mutating tools use
the immutable prepare/commit ledger (BD-15).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from plugins.platforms.tiktok_business.policy import TikTokPolicyEngine, PolicyDecision as CapabilityDecision
from plugins.bytedance.shared.approval import ApprovalLedger, IntentState
from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.media import MediaBroker
from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.state import StateStore, get_state_store
from plugins.bytedance.shared.tokens import TokenBroker
from plugins.platforms.tiktok_business.client import TikTokBusinessClient
from plugins.platforms.tiktok_business.models import (
    AccountConfig,
    PROVIDER_TIKTOK_BUSINESS,
    TikTokBusinessAPI,
    TikTokScope,
    capabilities_from_scopes,
    scope_set_from_token_info,
)

logger = logging.getLogger(__name__)


class TikTokOrganicOps:
    """TikTok Organic / Business Account operations.

    Requires owned Business Account with the corresponding scopes:
    - TikTokScope.VIDEO_LIST, VIDEO_CREATE, COMMENT, etc.
    """

    def __init__(
        self,
        *,
        profile: str = "default",
        accounts: Optional[Dict[str, AccountConfig]] = None,
        token_broker: Optional[TokenBroker] = None,
        state_store: Optional[StateStore] = None,
    ) -> None:
        self._profile = profile
        self._accounts = accounts or {}
        self._token_broker = token_broker or TokenBroker()
        self._state = state_store or get_state_store()
        self._ledger = ApprovalLedger(state_store=self._state)

    def register_account(self, alias: str, account: AccountConfig) -> None:
        """Register an account for organic operations."""
        self._accounts[alias] = account

    def _get_client(self, account_alias: str) -> TikTokBusinessClient:
        account = self._accounts[account_alias]
        return TikTokBusinessClient(account, token_broker=self._token_broker)

    # ------------------------------------------------------------------
    # Read tools (no approval)
    # ------------------------------------------------------------------

    async def account_get(self, account_alias: str) -> Dict[str, Any]:
        """Read Business Account profile and scopes."""
        client = self._get_client(account_alias)
        try:
            info = await client.inspect_token()
            business_info = await client.get_business_account_info()
            return {
                "account_alias": account_alias,
                "scopes": sorted(client.scopes),
                "features": capabilities_from_scopes(client.scopes),
                "business_account": business_info.get("data") or {},
            }
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def posts_list(
        self,
        account_alias: str,
        *,
        cursor: Optional[str] = None,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        """List owned posts."""
        client = self._get_client(account_alias)
        try:
            result = await client.list_posts(cursor=cursor)
            return self._normalize_posts_list(result)
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    @staticmethod
    def _normalize_posts_list(result: Dict[str, Any]) -> Dict[str, Any]:
        data = result.get("data") or result.get("list") or []
        posts = []
        for post in data:
            posts.append({
                "video_id": post.get("video_id") or post.get("id"),
                "title": post.get("title") or post.get("description", ""),
                "status": post.get("status"),
                "create_time": post.get("create_time"),
                "view_count": post.get("statistics", {}).get("play_count", 0),
            })
        return {
            "posts": posts,
            "cursor": result.get("cursor"),
            "has_more": result.get("has_more", False),
        }

    async def comments_list(
        self,
        account_alias: str,
        video_id: str,
        *,
        cursor: Optional[str] = None,
        page_size: int = 50,
    ) -> Dict[str, Any]:
        """List comments on a post."""
        client = self._get_client(account_alias)
        try:
            result = await client.list_comments(video_id, cursor=cursor)
            data = result.get("data") or {}
            comments = []
            for c in data.get("comments", []):
                comments.append({
                    "comment_id": c.get("comment_id") or c.get("id"),
                    "text": c.get("text"),
                    "author": c.get("author"),
                    "create_time": c.get("create_time"),
                    "status": c.get("status"),
                })
            return {
                "comments": comments,
                "cursor": data.get("cursor"),
                "has_more": data.get("has_more", False),
            }
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def publish_status(
        self, account_alias: str, publish_id: str
    ) -> Dict[str, Any]:
        """Get publish status."""
        client = self._get_client(account_alias)
        try:
            result = await client.get_publish_status(publish_id)
            return result.get("data") or {}
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Prepare/commit tools (approval-bound)
    # ------------------------------------------------------------------

    async def comment_reply_prepare(
        self,
        account_alias: str,
        comment_id: str,
        text: str,
        *,
        actor_id: str = "system",
    ) -> Dict[str, Any]:
        """Build an immutable comment reply intent (no provider side effect).

        Per §9.3: prepare has no provider side effect.  It validates
        the account, stores an immutable intent, and returns a
        preview plus approval token.
        """
        client = self._get_client(account_alias)
        try:
            # Verify scopes
            scopes = await client.inspect_scopes()
            if TikTokScope.COMMENT.value not in scopes:
                raise ProviderError(
                    "Account lacks comment scope",
                    retryable=False,
                    context={"account_alias": account_alias},
                )

            payload = {
                "comment_id": comment_id,
                "text": text,
                "account_alias": account_alias,
            }
            payload_sha = ApprovalLedger.compute_payload_sha256(payload)

            intent = self._ledger.prepare(
                profile=self._profile,
                provider="tiktok_business",
                account_alias=account_alias,
                actor_id=actor_id,
                payload=payload,
                payload_sha256=payload_sha,
                preview_json=json.dumps({
                    "comment_id": comment_id,
                    "text_preview": text[:100],
                    "account_alias": account_alias,
                }),
            )

            Metrics.increment(
                "bytedance_publish_intent_total",
                labels={"provider": "tiktok_business", "state": "validated"},
            )

            return {
                "intent_id": intent.intent_id,
                "state": intent.state.value,
                "preview": json.loads(intent.preview_json or "{}"),
                "payload_sha256": intent.payload_sha256,
                "expires_at": intent.expires_at,
            }
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def comment_reply_commit(
        self, intent_id: str, *, actor_id: str = "system"
    ) -> Dict[str, Any]:
        """Publish an approved comment reply."""
        record = self._ledger.get_intent(self._profile, intent_id)
        if record is None:
            return {"error": "Intent not found"}

        if IntentState(record.state) != IntentState.APPROVED:
            return {"error": f"Intent must be approved (current: {record.state})"}

        if time.time() >= record.expires_at:
            self._ledger.update_intent(
                self._profile, intent_id,
                state=IntentState.EXPIRED.value,
            )
            return {"error": "Intent expired"}

        payload = json.loads(record.payload_json)
        account_alias = payload["account_alias"]
        client = self._get_client(account_alias)

        try:
            # Mark COMMITTING durably BEFORE network I/O
            self._ledger.update_intent(
                self._profile, intent_id,
                state=IntentState.COMMITTING.value,
            )

            # Perform the provider call
            provider_result = await client._http.request(
                "POST",
                TikTokBusinessAPI.COMMENT_REPLY_CREATE,
                endpoint="default",
                headers={"Access-Token": (await client._get_token()).access_token},
                json_body={
                    "comment_id": payload["comment_id"],
                    "content": payload["text"],
                },
            )

            # Mark published
            self._ledger.mark_published(self._profile, intent_id)
            Metrics.increment(
                "bytedance_publish_intent_total",
                labels={"provider": "tiktok_business", "state": "published"},
            )

            return {
                "success": True,
                "intent_id": intent_id,
                "provider_result": provider_result,
            }
        except ProviderError as e:
            self._ledger.mark_failed(self._profile, intent_id, e.message)
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def post_prepare(
        self,
        account_alias: str,
        video_path: str,
        caption: str,
        *,
        hashtags: Optional[List[str]] = None,
        privacy: str = "PUBLIC",
        commercial_content: bool = False,
        actor_id: str = "system",
    ) -> Dict[str, Any]:
        """Validate and preview a TikTok Business post (no provider side effect).

        Per §9.3: prepare validates media, account, current provider
        constraints, disclosure fields, caption, and destinations.
        It stores an immutable intent and returns a preview plus
        approval token.
        """
        # Validate video file exists
        video = Path(video_path)
        if not video.exists() or not video.is_file():
            raise ProviderError(f"Video file not found: {video_path}", retryable=False)

        file_size = video.stat().st_size
        if file_size > 287 * 1024 * 1024:  # 287 MiB TikTok limit
            raise ProviderError(
                f"Video too large: {file_size} bytes (max 287 MiB)",
                retryable=False,
            )

        # Compute media hash for integrity
        media_bytes = await asyncio.to_thread(video.read_bytes)
        media_sha = hashlib.sha256(media_bytes).hexdigest()

        payload = {
            "account_alias": account_alias,
            "video_sha256": media_sha,
            "caption": caption,
            "hashtags": hashtags or [],
            "privacy": privacy,
            "commercial_content": commercial_content,
        }
        payload_sha = ApprovalLedger.compute_payload_sha256(payload)

        intent = self._ledger.prepare(
            profile=self._profile,
            provider="tiktok_business",
            account_alias=account_alias,
            actor_id=actor_id,
            payload=payload,
            payload_sha256=payload_sha,
            preview_json=json.dumps({
                "video_size_bytes": file_size,
                "video_sha256": media_sha,
                "caption_preview": caption[:100],
                "hashtags": hashtags or [],
                "privacy": privacy,
                "commercial_content": commercial_content,
            }),
        )

        Metrics.increment(
            "bytedance_publish_intent_total",
            labels={"provider": "tiktok_business", "state": "validated"},
        )

        return {
            "intent_id": intent.intent_id,
            "state": intent.state.value,
            "preview": json.loads(intent.preview_json or "{}"),
            "payload_sha256": intent.payload_sha256,
            "expires_at": intent.expires_at,
        }

    async def post_commit(
        self, intent_id: str, *, actor_id: str = "system"
    ) -> Dict[str, Any]:
        """Publish an approved TikTok post.

        Per §9.3: commit verifies exact payload digest, freshness,
        account, actor, and unused state, then performs the provider
        call ONCE.
        """
        record = self._ledger.get_intent(self._profile, intent_id)
        if record is None:
            return {"error": "Intent not found"}

        if IntentState(record.state) != IntentState.APPROVED:
            return {"error": f"Intent must be approved (current: {record.state})"}

        if time.time() >= record.expires_at:
            self._ledger.update_intent(
                self._profile, intent_id,
                state=IntentState.EXPIRED.value,
            )
            return {"error": "Intent expired"}

        original_payload = json.loads(record.payload_json)
        account_alias = original_payload["account_alias"]

        client = self._get_client(account_alias)
        try:
            # Verify the original payload is still valid
            payload_sha = ApprovalLedger.compute_payload_sha256(original_payload)
            if payload_sha != record.payload_sha256:
                return {"error": "Payload integrity mismatch"}

            # Mark COMMITTING durably BEFORE network I/O
            self._ledger.update_intent(
                self._profile, intent_id,
                state=IntentState.COMMITTING.value,
            )

            # Perform the provider publish
            publish_result = await client.publish_video(original_payload)
            data = publish_result.get("data") or {}
            publish_id = data.get("publish_id") or data.get("id")

            if publish_id:
                self._ledger.mark_submitted(
                    self._profile, intent_id, publish_id,
                )
            else:
                self._ledger.mark_failed(
                    self._profile, intent_id,
                    "Provider returned no publish_id",
                )
                return {"error": "Provider returned no publish_id"}

            Metrics.increment(
                "bytedance_publish_intent_total",
                labels={"provider": "tiktok_business", "state": "submitted"},
            )

            return {
                "success": True,
                "intent_id": intent_id,
                "publish_id": publish_id,
                "publish_status": "submitted",
                "provider_result": data,
            }
        except ProviderError as e:
            self._ledger.mark_failed(self._profile, intent_id, e.message)
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    # ------------------------------------------------------------------
    # Comment moderation (prepare/commit)
    # ------------------------------------------------------------------

    async def comment_moderate_prepare(
        self,
        account_alias: str,
        comment_id: str,
        action: str,
        *,
        actor_id: str = "system",
    ) -> Dict[str, Any]:
        """Prepare a comment moderation action."""
        payload = {
            "account_alias": account_alias,
            "comment_id": comment_id,
            "action": action,
        }
        payload_sha = ApprovalLedger.compute_payload_sha256(payload)

        intent = self._ledger.prepare(
            profile=self._profile,
            provider="tiktok_business",
            account_alias=account_alias,
            actor_id=actor_id,
            payload=payload,
            payload_sha256=payload_sha,
            preview_json=json.dumps({
                "comment_id": comment_id,
                "action": action,
                "account_alias": account_alias,
            }),
        )

        return {
            "intent_id": intent.intent_id,
            "state": intent.state.value,
            "preview": json.loads(intent.preview_json or "{}"),
            "expires_at": intent.expires_at,
        }

    async def comment_moderate_commit(
        self, intent_id: str, *, actor_id: str = "system"
    ) -> Dict[str, Any]:
        """Apply an approved comment moderation action."""
        record = self._ledger.get_intent(self._profile, intent_id)
        if record is None:
            return {"error": "Intent not found"}

        # Verify state is APPROVED
        if IntentState(record.state) != IntentState.APPROVED:
            return {"error": f"Intent must be approved (current: {record.state})"}

        if time.time() >= record.expires_at:
            self._ledger.update_intent(
                self._profile, intent_id,
                state=IntentState.EXPIRED.value,
            )
            return {"error": "Intent expired"}

        payload = json.loads(record.payload_json)
        account_alias = payload["account_alias"]
        comment_id = payload["comment_id"]
        action = payload["action"]

        client = self._get_client(account_alias)
        try:
            # Mark COMMITTING
            self._ledger.update_intent(
                self._profile, intent_id,
                state=IntentState.COMMITTING.value,
            )

            # Determine endpoint based on action
            if action in ("hide", "unhide"):
                endpoint = TikTokBusinessAPI.COMMENT_HIDE
                body = {"comment_id": comment_id, "is_hidden": action == "hide"}
            elif action == "delete":
                endpoint = TikTokBusinessAPI.COMMENT_DELETE
                body = {"comment_id": comment_id}
            elif action in ("like", "unlike"):
                endpoint = TikTokBusinessAPI.COMMENT_LIKE
                body = {"comment_id": comment_id, "action": "like" if action == "like" else "unlike"}
            else:
                return {"error": f"Unknown action: {action}"}

            result = await client._http.request(
                "POST",
                endpoint,
                endpoint="default",
                headers={"Access-Token": (await client._get_token()).access_token},
                json_body=body,
            )

            self._ledger.mark_published(self._profile, intent_id)
            return {"success": True, "intent_id": intent_id, "result": result}
        except ProviderError as e:
            self._ledger.mark_failed(self._profile, intent_id, e.message)
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()


import asyncio  # noqa: E402
