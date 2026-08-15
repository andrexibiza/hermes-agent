"""Douyin IM policy engine.

Per design spec §11.4: Douyin documents multiple messaging scenes
with different prerequisites and timing.  The adapter represents
them as explicit grants.  ``send()`` must select a valid grant and
atomically consume its local allowance.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

from plugins.platforms.douyin.models import DouyinSendGrant

logger = logging.getLogger(__name__)


@dataclass
class DouyinPolicyDecision:
    """Result of a Douyin send policy check."""

    allowed: bool
    reason_code: str
    grant: Optional[DouyinSendGrant] = None
    requires_new_grant: bool = False
    details: Optional[Dict[str, Any]] = None


class DouyinPolicyEngine:
    """Scene-aware send policy for Douyin IM.

    Per §11.4 and §4.7:
    - reply/enter/B2B scene grants are explicit
    - expired or ineligible grants are denied
    - grant consumption is atomic
    - cron standalone send fails closed without a valid grant
    """

    def check_send(
        self,
        conversation_short_id: str,
        *,
        client: Any,
        sender_id: Optional[str] = None,
        scopes: Optional[set[str]] = None,
        allow_all_users: bool = False,
        allowed_users: Optional[set[str]] = None,
    ) -> DouyinPolicyDecision:
        """Check if a send is permitted for the given conversation.

        Requires a valid, eligible send grant for the conversation.
        """
        # Check im.direct_message scope
        if scopes is not None:
            if "im.direct_message" not in scopes:
                return DouyinPolicyDecision(
                    allowed=False,
                    reason_code="missing_im_direct_message_scope",
                    details={"required_scope": "im.direct_message"},
                )

        # Check allowlist
        if sender_id and allowed_users is not None and not allow_all_users:
            if sender_id not in allowed_users:
                return DouyinPolicyDecision(
                    allowed=False,
                    reason_code="user_not_allowed",
                    details={"sender_id": sender_id},
                )

        # Check send grant
        grant = client.get_send_grant(conversation_short_id) if client else None
        if grant is None:
            return DouyinPolicyDecision(
                allowed=False,
                reason_code="no_send_grant",
                requires_new_grant=True,
                details={"scene": "unknown"},
            )

        if not grant.eligible:
            return DouyinPolicyDecision(
                allowed=False,
                reason_code="grant_ineligible",
                grant=grant,
                details={"reason": grant.reason},
            )

        if grant.expires_at and time.time() >= grant.expires_at.timestamp():
            return DouyinPolicyDecision(
                allowed=False,
                reason_code="grant_expired",
                grant=grant,
            )

        if grant.remaining_count is not None and grant.remaining_count <= 0:
            return DouyinPolicyDecision(
                allowed=False,
                reason_code="grant_exhausted",
                grant=grant,
            )

        return DouyinPolicyDecision(
            allowed=True,
            reason_code="ok",
            grant=grant,
        )


import time  # noqa: E402
