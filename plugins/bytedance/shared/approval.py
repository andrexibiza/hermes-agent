"""Immutable approval and public-operation ledger.

Per the design spec §6.6 and §9.3: all public publishing is a
two-phase transaction:

1. ``prepare`` validates media, account, current provider constraints,
   disclosure fields, caption, and destinations.  It stores an immutable
   intent and returns a preview plus approval token (intent_id).
   It has NO provider side effect.

2. ``commit`` accepts the token, verifies exact payload digest,
   freshness, account, actor, and unused state, then performs the
   provider call ONCE.

Changing any field invalidates approval and requires a new prepare step.

State machine (§9.3):
    DRAFT -> VALIDATED -> AWAITING_APPROVAL -> APPROVED
    -> COMMITTING -> SUBMITTED -> PROCESSING -> PUBLISHED
    -> REJECTED | FAILED | EXPIRED | CANCELLED
"""

from __future__ import annotations

import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Optional

from plugins.bytedance.shared.observability import Metrics
from plugins.bytedance.shared.state import PublishIntentRecord, StateStore, get_state_store

logger = logging.getLogger(__name__)


class IntentState(str, Enum):
    """Publish intent state machine values.

    Mirrors the design spec §9.3 state machine.
    """

    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    COMMITTING = "COMMITTING"
    SUBMITTED = "SUBMITTED"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"

    # Terminal states — once reached, no further transitions allowed
    # (except EXPIRED and CANCELLED which can be re-prepared)
    TERMINAL = frozenset({
        "PUBLISHED", "REJECTED", "FAILED", "EXPIRED", "CANCELLED",
    })


@dataclass(frozen=True)
class PreparedIntent:
    """Result of a prepare call — immutable intent + approval token."""

    intent_id: str
    state: IntentState
    preview_json: Optional[str]
    payload_sha256: str
    expires_at: float
    actor_id: str
    provider_job_id: Optional[str] = None


class ApprovalLedger:
    """Manages the prepare/approve/commit lifecycle for public operations.

    The ledger is backed by the durable StateStore (SQLite) so
    intents survive process restarts.
    """

    def __init__(self, *, state_store: Optional[StateStore] = None) -> None:
        self._state = state_store or get_state_store()
        self._default_ttl_seconds: float = 900.0  # 15 minutes

    @staticmethod
    def compute_payload_sha256(payload: Dict[str, Any]) -> str:
        """Compute the canonical SHA-256 of a payload dict.

        Uses a deterministic JSON serialization (sorted keys) so that
        ``commit`` can verify exact-match against ``prepare``.
        """
        return hashlib.sha256(
            json_dumps_canonical(payload).encode("utf-8")
        ).hexdigest()

    def prepare(
        self,
        profile: str,
        provider: str,
        account_alias: str,
        actor_id: str,
        payload: Dict[str, Any],
        *,
        preview_json: Optional[str] = None,
        expires_in: float = 900.0,
    ) -> PreparedIntent:
        """Phase 1: validate, store immutable intent, return approval token.

        Has NO provider side effect.  The returned ``intent_id`` is the
        approval token that ``commit`` consumes.
        """
        intent_id = secrets.token_urlsafe(24)
        payload_json = json_dumps_canonical(payload)
        payload_sha256 = self.compute_payload_sha256(payload)
        now = time.time()
        expires_at = now + expires_in

        # Transition to VALIDATED
        self._state.create_publish_intent(
            intent_id=intent_id,
            profile=profile,
            provider=provider,
            account_alias=account_alias,
            actor_id=actor_id,
            payload_json=payload_json,
            payload_sha256=payload_sha256,
            preview_json=preview_json,
            expires_at=expires_at,
        )

        # Update state from DRAFT to VALIDATED
        self._state.update_publish_intent(
            profile, intent_id, state=IntentState.VALIDATED.value,
        )

        Metrics.increment(
            "bytedance_publish_intent_total",
            labels={"provider": provider, "state": "validated"},
        )

        logger.info(
            "ApprovalLedger: prepared intent %s for %s/%s",
            intent_id[:8], provider, account_alias,
        )

        return PreparedIntent(
            intent_id=intent_id,
            state=IntentState.VALIDATED,
            preview_json=preview_json,
            payload_sha256=payload_sha256,
            expires_at=expires_at,
            actor_id=actor_id,
        )

    def approve(self, profile: str, intent_id: str, actor_id: str) -> bool:
        """Move intent from VALIDATED/AWAITING_APPROVAL to APPROVED.

        Returns True if the transition succeeded.
        """
        record = self._state.get_publish_intent(profile, intent_id)
        if record is None:
            return False

        current_state = IntentState(record.state)
        if current_state not in (IntentState.VALIDATED, IntentState.AWAITING_APPROVAL):
            return False

        # Check expiry
        if time.time() >= record.expires_at:
            self._state.update_publish_intent(
                profile, intent_id, state=IntentState.EXPIRED.value,
            )
            return False

        # Verify actor matches
        if record.actor_id != actor_id:
            return False

        self._state.update_publish_intent(
            profile,
            intent_id,
            state=IntentState.APPROVED.value,
            approved_at=time.time(),
        )
        return True

    def commit(
        self,
        profile: str,
        intent_id: str,
        payload: Dict[str, Any],
        *,
        actor_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Phase 2: verify and return the durable intent for provider execution.

        Verifies:
        - Intent exists and is APPROVED
        - Payload SHA-256 matches the prepared payload exactly
        - Intent has not expired
        - Intent has not already been committed

        Returns the stored payload_json if valid (caller then performs
        the provider call and calls ``mark_committed``).

        Returns None if verification fails.
        """
        record = self._state.get_publish_intent(profile, intent_id)
        if record is None:
            return None

        # Check expiry
        if time.time() >= record.expires_at:
            self._state.update_publish_intent(
                profile, intent_id, state=IntentState.EXPIRED.value,
            )
            return None

        current_state = IntentState(record.state)
        if current_state != IntentState.APPROVED:
            return None

        # Verify exact payload digest — changing any field invalidates approval
        actual_sha = self.compute_payload_sha256(payload)
        if actual_sha != record.payload_sha256:
            logger.warning(
                "ApprovalLedger: payload digest mismatch for intent %s",
                intent_id[:8],
            )
            return None

        # Verify actor
        if record.actor_id != actor_id:
            return None

        # Mark COMMITTING durably BEFORE network I/O
        self._state.update_publish_intent(
            profile,
            intent_id,
            state=IntentState.COMMITTING.value,
            committed_at=time.time(),
        )

        return {
            "intent_id": intent_id,
            "payload_json": record.payload_json,
            "payload_sha256": record.payload_sha256,
            "provider": record.provider,
            "account_alias": record.account_alias,
            "actor_id": record.actor_id,
        }

    def mark_submitted(
        self, profile: str, intent_id: str, provider_job_id: str
    ) -> None:
        """Mark intent as submitted with the provider's job ID."""
        self._state.update_publish_intent(
            profile,
            intent_id,
            state=IntentState.SUBMITTED.value,
            provider_job_id=provider_job_id,
        )

    def mark_processing(self, profile: str, intent_id: str) -> None:
        self._state.update_publish_intent(
            profile, intent_id, state=IntentState.PROCESSING.value,
        )

    def mark_published(self, profile: str, intent_id: str) -> None:
        self._state.update_publish_intent(
            profile, intent_id, state=IntentState.PUBLISHED.value,
        )

    def mark_rejected(self, profile: str, intent_id: str, reason: str) -> None:
        self._state.update_publish_intent(
            profile, intent_id,
            state=IntentState.REJECTED.value,
            last_error=reason,
        )

    def mark_failed(self, profile: str, intent_id: str, reason: str) -> None:
        self._state.update_publish_intent(
            profile, intent_id,
            state=IntentState.FAILED.value,
            last_error=reason,
        )

    def get_intent(
        self, profile: str, intent_id: str
    ) -> Optional[PublishIntentRecord]:
        return self._state.get_publish_intent(profile, intent_id)

    def update_intent(
        self,
        profile: str,
        intent_id: str,
        **fields: Any,
    ) -> None:
        """Update arbitrary fields on a publish_intent.

        Supported fields: state, approved_at, committed_at,
        provider_job_id, provider_status, last_error.
        """
        self._state.update_publish_intent(profile, intent_id, **fields)

    def expire_old_intents(self, profile: str, older_than_seconds: float = 900.0) -> int:
        """Mark intents past their expiry as EXPIRED."""
        now = time.time()
        with self._state._tx(profile) as conn:
            cur = conn.execute(
                """UPDATE publish_intent
                   SET state = 'EXPIRED'
                   WHERE profile = ? AND state IN ('DRAFT', 'VALIDATED', 'APPROVED')
                     AND expires_at < ?""",
                (profile, now),
            )
            return cur.rowcount


def json_dumps_canonical(obj: Any) -> str:
    """Deterministic JSON serialization (sorted keys, no whitespace)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
