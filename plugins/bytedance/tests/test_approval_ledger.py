"""Test: immutable approval ledger (BD-15).

Per design spec §6.6 and §9.3: all public publishing is a
two-phase transaction: prepare → approve → commit.  This test
verifies the full lifecycle.
"""

import json
from pathlib import Path

import pytest

from plugins.bytedance.shared.approval import (
    ApprovalLedger,
    IntentState,
)
from plugins.bytedance.shared.state import StateStore


@pytest.fixture
def ledger(tmp_path: Path) -> ApprovalLedger:
    store = StateStore(db_dir=tmp_path / "test_state")
    return ApprovalLedger(state_store=store)


@pytest.fixture
def profile() -> str:
    return "test_profile"


@pytest.fixture
def sample_payload() -> dict:
    return {
        "account_alias": "biz_1",
        "caption": "Test caption",
        "video_sha256": "abc123",
    }


class TestApprovalLedger:
    def test_prepare_creates_validated_intent(
        self, ledger, profile, sample_payload
    ):
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
            preview_json=json.dumps({"preview": "test"}),
        )
        assert intent.state == IntentState.VALIDATED
        assert intent.actor_id == "user_alice"
        assert intent.payload_sha256 is not None
        assert intent.expires_at is not None

    def test_approve_transitions_to_approved(
        self, ledger, profile, sample_payload
    ):
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
        )
        assert ledger.approve(profile, intent.intent_id, "user_alice") is True
        record = ledger.get_intent(profile, intent.intent_id)
        assert record.state == IntentState.APPROVED.value
        assert record.approved_at is not None

    def test_approve_fails_for_wrong_actor(
        self, ledger, profile, sample_payload
    ):
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
        )
        # approve() requires actor_id == stored actor_id
        assert ledger.approve(profile, intent.intent_id, "user_bob") is False

    def test_commit_requires_approved(
        self, ledger, profile, sample_payload
    ):
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
        )
        # Not approved yet
        result = ledger.commit(profile, intent.intent_id, sample_payload, actor_id="user_alice")
        assert result is None  # commit returns None when not APPROVED

    def test_commit_succeeds_after_approval(
        self, ledger, profile, sample_payload
    ):
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
        )
        ledger.approve(profile, intent.intent_id, "user_alice")
        result = ledger.commit(profile, intent.intent_id, sample_payload, actor_id="user_alice")
        assert result is not None  # Returns the payload_json
        record = ledger.get_intent(profile, intent.intent_id)
        assert record.state == IntentState.COMMITTING.value

    def test_payload_mismatch_rejects_commit(
        self, ledger, profile, sample_payload
    ):
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
        )
        ledger.approve(profile, intent.intent_id, "user_alice")
        # Different payload
        tampered = dict(sample_payload, caption="CHANGED")
        result = ledger.commit(profile, intent.intent_id, tampered, actor_id="user_alice")
        assert result is None  # SHA mismatch

    def test_double_commit_prevented(
        self, ledger, profile, sample_payload
    ):
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
        )
        ledger.approve(profile, intent.intent_id, "user_alice")
        # First commit succeeds
        result = ledger.commit(profile, intent.intent_id, sample_payload, actor_id="user_alice")
        assert result is not None
        # Second commit fails — already COMMITTING
        result2 = ledger.commit(profile, intent.intent_id, sample_payload, actor_id="user_alice")
        assert result2 is None

    def test_expired_intent_cant_commit(self, ledger, profile, sample_payload):
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
        )
        # Manually set expires_at to past
        ledger.update_intent(profile, intent.intent_id, expires_at=100)
        # approve should fail (expired)
        assert ledger.approve(profile, intent.intent_id, "user_alice") is False

    def test_payload_sha256_is_deterministic(self):
        payload = {"a": 1, "b": "test", "c": [1, 2, 3]}
        sha1 = ApprovalLedger.compute_payload_sha256(payload)
        sha2 = ApprovalLedger.compute_payload_sha256(payload.copy())
        assert sha1 == sha2

    def test_different_payloads_different_sha(self):
        p1 = {"caption": "hello", "video_sha256": "abc"}
        p2 = {"caption": "world", "video_sha256": "abc"}
        sha1 = ApprovalLedger.compute_payload_sha256(p1)
        sha2 = ApprovalLedger.compute_payload_sha256(p2)
        assert sha1 != sha2

    def test_intent_isolation_by_profile(self, ledger, profile, sample_payload):
        """Intents must be scoped by profile — never leak across profiles."""
        intent = ledger.prepare(
            profile=profile,
            provider="tiktok_business",
            account_alias="biz_1",
            actor_id="user_alice",
            payload=sample_payload,
        )
        # Cannot find it under a different profile
        assert ledger.get_intent("other_profile", intent.intent_id) is None
