"""TikTok creator-authorized posting backend (BD-20).

Per design spec §10.3: when the TikTok Creator Authorization Flow
is configured (via TikTok Business Center), the creator's account
is treated as a distinct authorization domain.  Posting uses the
TikTok Content Posting API through the creator-authorized token.

This backend is optional — it is only enabled when
``tiktok_creator_enabled: true`` and the creator OAuth flow is
configured.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from plugins.bytedance.shared.approval import ApprovalLedger, IntentState
from plugins.bytedance.shared.errors import ProviderError
from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.state import StateStore, get_state_store
from plugins.platforms.tiktok_business.client import TikTokBusinessClient
from plugins.platforms.tiktok_business.models import (
    AccountConfig,
    PROVIDER_TIKTOK_BUSINESS,
    TikTokBusinessAPI,
)

logger = logging.getLogger(__name__)


class TikTokCreatorOps:
    """Creator-authorized posting backend.

    Uses TikTok's Content Posting API with creator-authorized tokens.
    All posting is immutable prepare/commit (BD-15).
    """

    def __init__(
        self,
        *,
        profile: str = "default",
        accounts: Optional[Dict[str, AccountConfig]] = None,
        ledger: Optional[ApprovalLedger] = None,
    ) -> None:
        self._profile = profile
        self._accounts = accounts or {}
        self._state = get_state_store()
        self._ledger = ledger or ApprovalLedger(state_store=self._state)

    def register_account(self, alias: str, account: AccountConfig) -> None:
        self._accounts[alias] = account

    def _get_client(self, account_alias: str) -> TikTokBusinessClient:
        account = self._accounts[account_alias]
        return TikTokBusinessClient(account)

    async def creator_connect(
        self,
        account_alias: str,
        redirect_uri: str,
    ) -> Dict[str, Any]:
        """Start the TikTok Creator OAuth flow.

        Returns the authorization URL the creator must visit.
        """
        account = self._accounts.get(account_alias)
        if account is None:
            return {"error": f"Unknown account: {account_alias}"}

        client = TikTokBusinessClient(account)
        try:
            auth_url = client.get_creator_auth_url(
                redirect_uri=redirect_uri,
                state=f"creator_{int(time.time())}",
            )
            return {
                "auth_url": auth_url,
                "account_alias": account_alias,
                "redirect_uri": redirect_uri,
            }
        except ProviderError as e:
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()

    async def creator_post_prepare(
        self,
        account_alias: str,
        video_path: str,
        caption: str,
        *,
        privacy: str = "PUBLIC",
        comments: bool = True,
        duet: bool = True,
        stitch: bool = True,
        commercial_content: bool = False,
        actor_id: str = "system",
    ) -> Dict[str, Any]:
        """Prepare a creator-authorized post (no provider side effect).

        Validates the video file and stores an immutable intent
        referencing the creator-authorized account.
        """
        import hashlib
        from pathlib import Path

        video = Path(video_path)
        if not video.exists():
            raise ProviderError(f"Video not found: {video_path}", retryable=False)

        file_size = video.stat().st_size
        if file_size > 287 * 1024 * 1024:
            raise ProviderError(
                f"Video too large ({file_size} bytes, max 287 MiB)",
                retryable=False,
            )

        video_bytes = video.read_bytes()
        video_sha = hashlib.sha256(video_bytes).hexdigest()

        payload = {
            "account_alias": account_alias,
            "video_sha256": video_sha,
            "caption": caption,
            "privacy": privacy,
            "comments": comments,
            "duet": duet,
            "stitch": stitch,
            "commercial_content": commercial_content,
        }
        payload_sha = ApprovalLedger.compute_payload_sha256(payload)

        intent = self._ledger.prepare(
            profile=self._profile,
            provider="tiktok_creator",
            account_alias=account_alias,
            actor_id=actor_id,
            payload=payload,
            payload_sha256=payload_sha,
            preview_json=json.dumps({
                "video_size": file_size,
                "video_sha256": video_sha,
                "caption_preview": caption[:100],
                "privacy": privacy,
                "commercial_content": commercial_content,
            }),
        )

        Metrics.increment(
            "bytedance_publish_intent_total",
            labels={"provider": "tiktok_creator", "state": "validated"},
        )

        return {
            "intent_id": intent.intent_id,
            "state": intent.state.value,
            "preview": json.loads(intent.preview_json or "{}"),
            "expires_at": intent.expires_at,
        }

    async def creator_post_commit(
        self, intent_id: str, *, actor_id: str = "system"
    ) -> Dict[str, Any]:
        """Commit a creator-authorized post.

        Requires the intent to be in APPROVED state.
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

        payload = json.loads(record.payload_json)
        account_alias = payload["account_alias"]
        client = self._get_client(account_alias)

        try:
            self._ledger.update_intent(
                self._profile, intent_id,
                state=IntentState.COMMITTING.value,
            )

            # Upload video
            from pathlib import Path
            video_data = Path(payload.get("video_path", "")).read_bytes() \
                if payload.get("video_path") else b""

            # Use the creator content posting API
            result = await client._http.request(
                "POST",
                TikTokBusinessAPI.CREATOR_POST,
                endpoint="default",
                headers={
                    "Authorization": f"Bearer {await client._get_token().access_token}",
                },
                json_body={
                    "video": {
                        "video_size": len(video_data),
                        "sha256": payload["video_sha256"],
                    },
                    "caption": payload["caption"],
                    "privacy": {"option": payload["privacy"]},
                    "comment": {"allow_comment": payload["comments"]},
                    "duet": {"allow_duet": payload["duet"]},
                    "stitch": {"allow_stitch": payload["stitch"]},
                    "brand_survey": payload["commercial_content"],
                },
            )

            data = result.get("data") or {}
            publish_id = data.get("publish_id")

            if publish_id:
                self._ledger.mark_submitted(self._profile, intent_id, publish_id)
                Metrics.increment(
                    "bytedance_publish_intent_total",
                    labels={"provider": "tiktok_creator", "state": "submitted"},
                )
                return {"success": True, "intent_id": intent_id, "publish_id": publish_id}
            else:
                self._ledger.mark_failed(
                    self._profile, intent_id,
                    "Provider returned no publish_id",
                )
                return {"error": "Provider returned no publish_id"}

        except ProviderError as e:
            self._ledger.mark_failed(self._profile, intent_id, e.message)
            return {"error": e.message, "status": e.status}
        finally:
            await client.close()
