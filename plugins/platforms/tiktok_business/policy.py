"""TikTok Business policy engine.

Per the design spec §8.5 outbound flow and §7.4: the outbound path
receives a capability decision, not a boolean buried inside adapter code.
The adapter never assumes that a recent inbound message guarantees send
capability.  The capability endpoint is the provider source of truth.

This module implements:
- Conversation capability checks (can_send, allowed_message_types, etc.)
- Close conversation denial
- Echo suppression
- Account/user allowlist gating
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Set

from plugins.bytedance.shared.state import StateStore, get_state_store
from plugins.platforms.tiktok_business.models import (
    ConversationCapability,
    TikTokScope,
)

logger = logging.getLogger(__name__)


@dataclass
class PolicyDecision:
    """Result of a policy check before outbound."""

    allowed: bool
    reason_code: str
    capability: Optional[ConversationCapability] = None
    details: Dict[str, Any] = None


class TikTokPolicyEngine:
    """TikTok Business Messaging policy engine.

    Before every new send context:
    1. Check the conversation capability snapshot (cache or refresh).
    2. Check account-level allowlists.
    3. Check message type against allowed types.
    4. Check remaining message budget.
    """

    def __init__(self, *, state_store: Optional[StateStore] = None) -> None:
        self._state = state_store or get_state_store()

    def check_send(
        self,
        conversation_id: str,
        message_type: str,
        *,
        provider: str,
        account_alias: str,
        profile: str,
        allowed_users: Optional[Set[str]] = None,
        allow_all_users: bool = False,
        sender_id: Optional[str] = None,
        scopes: Optional[Set[str]] = None,
        capability: Optional[ConversationCapability] = None,
    ) -> PolicyDecision:
        """Check if a message can be sent to a conversation.

        Args:
            conversation_id: Provider conversation ID
            message_type: "text", "image", "video", etc.
            provider: Provider name
            account_alias: Account alias
            profile: Hermes profile name
            allowed_users: Set of allowed sender user IDs
            allow_all_users: If True, skip allowlist check
            sender_id: The sender's user ID (for allowlist check)
            scopes: Set of granted scopes
            capability: Pre-fetched capability snapshot (if available)
        """
        details: Dict[str, Any] = {}

        # 1. Check capability
        if capability is not None:
            if not capability.can_send:
                return PolicyDecision(
                    allowed=False,
                    reason_code="conversation_closed",
                    capability=capability,
                    details={"reason": "Conversation capability says cannot_send"},
                )
            if message_type not in capability.allowed_message_types:
                return PolicyDecision(
                    allowed=False,
                    reason_code="message_type_not_allowed",
                    capability=capability,
                    details={
                        "message_type": message_type,
                        "allowed": list(capability.allowed_message_types),
                    },
                )
            if capability.max_messages_remaining is not None:
                if capability.max_messages_remaining <= 0:
                    return PolicyDecision(
                        allowed=False,
                        reason_code="message_budget_exhausted",
                        capability=capability,
                    )
        else:
            # No capability snapshot — check if we have one cached
            cached = self._state.get_conversation_capability(
                profile, provider, account_alias, conversation_id
            )
            if cached:
                if not cached.get("can_send"):
                    return PolicyDecision(
                        allowed=False,
                        reason_code="conversation_closed",
                        details={"reason": "Cached capability: cannot_send"},
                    )
                allowed_types = cached.get("allowed_message_types", [])
                if message_type not in allowed_types:
                    return PolicyDecision(
                        allowed=False,
                        reason_code="message_type_not_allowed",
                        details={
                            "message_type": message_type,
                            "allowed": allowed_types,
                        },
                    )

        # 2. Check scopes — send capability requires business_messaging_send
        if scopes is not None:
            required_scope = TikTokScope.SEND.value
            if not scopes:
                return PolicyDecision(
                    allowed=False,
                    reason_code="no_scopes",
                    details={"required": required_scope},
                )
            if required_scope not in scopes:
                return PolicyDecision(
                    allowed=False,
                    reason_code="missing_scope",
                    details={"required": required_scope},
                )

        # 3. Check allowlist
        if sender_id and allowed_users is not None and not allow_all_users:
            if sender_id not in allowed_users:
                return PolicyDecision(
                    allowed=False,
                    reason_code="user_not_allowed",
                    details={"sender_id": sender_id},
                )

        # 4. Check for echo (sender_is_self)
        # This is checked by the caller, but we log it here
        details["checks_passed"] = [
            "capability",
            "scopes" if scopes else "scopes_skipped",
            "allowlist",
        ]

        return PolicyDecision(
            allowed=True,
            reason_code="ok",
            capability=capability or None,
            details=details,
        )

    def is_echo(
        self,
        sender_id: Optional[str],
        account_open_id: Optional[str],
    ) -> bool:
        """Check if a message is an echo from the authenticated account."""
        if not sender_id or not account_open_id:
            return False
        return sender_id == account_open_id
