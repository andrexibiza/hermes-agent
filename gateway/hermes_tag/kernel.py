"""Composed Hermes Tag governance kernel."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from .capability import CapabilityRegistry
from .config import HermesTagConfig
from .continuity import ContinuityStore
from .enforcement import LeaseAuthority
from .errors import LeaseError, LeaseTampered
from .identity import IdentityStore
from .ledger import BudgetLimits, HermesTagLedger, ReceiptRecord
from .middleware import AdmissionResult, TurnAdmissionMiddleware
from .model import (
    ActionIntent,
    CapabilityLease,
    ContinuityMode,
    DecisionOutcome,
    ExternalIdentity,
    Fact,
    PolicyDecision,
    Principal,
    RiskLevel,
    SurfaceRef,
    arguments_digest,
)
from .obligations import ObligationPhase, ObligationRegistry
from .omniscience import FactStore
from .policy import PolicyEngine, PolicyEvaluation, PolicyRule

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthorizationResult:
    intent: ActionIntent
    decision: PolicyDecision
    lease: CapabilityLease | None = None
    token: str | None = None


@dataclass(frozen=True, slots=True)
class CompletionResult:
    lease: CapabilityLease
    receipt: ReceiptRecord
    budget_state: str | None


class HermesTagKernel:
    """Single process facade over identity, continuity, policy, and audit."""

    def __init__(
        self,
        ledger: HermesTagLedger,
        config: HermesTagConfig,
        *,
        rules: Iterable[PolicyRule] = (),
        lease_authority: LeaseAuthority | None = None,
        capabilities: CapabilityRegistry | None = None,
        obligations: ObligationRegistry | None = None,
    ) -> None:
        self.ledger = ledger
        self.config = config
        self.identities = IdentityStore(ledger)
        self.continuities = ContinuityStore(ledger)
        self.facts = FactStore(ledger)
        self.capabilities = capabilities or CapabilityRegistry()
        self.obligations = obligations or ObligationRegistry()
        self.leases = lease_authority
        self.policy = PolicyEngine(
            ledger,
            rules=rules,
            budget_limits=BudgetLimits(
                hourly_tokens=config.budgets.hourly_tokens,
                daily_tokens=config.budgets.daily_tokens,
                hourly_cost_usd=config.budgets.hourly_cost_usd,
                daily_cost_usd=config.budgets.daily_cost_usd,
            ),
        )
        self.middleware = TurnAdmissionMiddleware(
            ledger,
            config,
            identities=self.identities,
            continuities=self.continuities,
            facts=self.facts,
        )

    def _rollback_failed_authorization(
        self,
        evaluation: PolicyEvaluation,
        *,
        now: datetime | None,
    ) -> None:
        reservation_id = evaluation.decision.budget_reservation_id
        if reservation_id is not None:
            try:
                self.ledger.release_budget(reservation_id, now=now)
            except Exception as exc:  # best-effort reconciliation; preserve root failure
                logger.error(
                    "Hermes Tag failed to release undelivered budget authority: %s",
                    type(exc).__name__,
                )
        if evaluation.approval is not None:
            try:
                self.policy.approvals.restore_consumed(
                    evaluation.approval, now=now
                )
            except Exception as exc:  # best-effort reconciliation; preserve root failure
                logger.error(
                    "Hermes Tag failed to restore undelivered approval authority: %s",
                    type(exc).__name__,
                )

    def admit_turn(
        self,
        identity: ExternalIdentity,
        surface: SurfaceRef,
        *,
        event_id: str | None = None,
        project_id: str | None = None,
        continuity_mode: ContinuityMode | None = None,
        explicit_continuity_id: str | None = None,
    ) -> AdmissionResult:
        return self.middleware.admit(
            identity,
            surface,
            event_id=event_id,
            project_id=project_id,
            continuity_mode=continuity_mode,
            explicit_continuity_id=explicit_continuity_id,
        )

    def authorize(
        self,
        principal: Principal,
        *,
        capability: str,
        action: str,
        resource: str,
        arguments: Mapping[str, Any] | tuple[Any, ...] | list[Any] | None,
        scope: Any,
        risk: RiskLevel | str | int = RiskLevel.LOW,
        external_effect: bool = False,
        network_egress: bool = False,
        state_write: bool = False,
        metadata: Mapping[str, Any] | None = None,
        projected_tokens: int | None = 0,
        projected_cost_usd: float | None = 0.0,
        now: datetime | None = None,
    ) -> AuthorizationResult:
        intent, definition = self.capabilities.normalize_intent(
            capability=capability,
            action=action,
            resource=resource,
            arguments_digest=arguments_digest(arguments),
            scope=scope,
            risk=risk,
            external_effect=external_effect,
            network_egress=network_egress,
            state_write=state_write,
            metadata=metadata,
        )
        evaluation: PolicyEvaluation = self.policy.evaluate(
            principal,
            intent,
            definition,
            projected_tokens=projected_tokens,
            projected_cost_usd=projected_cost_usd,
            now=now,
        )
        if evaluation.decision.outcome is not DecisionOutcome.ALLOW:
            return AuthorizationResult(intent=intent, decision=evaluation.decision)
        if self.leases is None:
            self._rollback_failed_authorization(evaluation, now=now)
            raise LeaseError(
                "policy allowed the action but no lease signing authority is configured"
            )
        try:
            lease, token = self.leases.issue(
                principal, intent, evaluation.decision, now=now
            )
            self.ledger.append_receipt(
                event_id=f"lease-issued:{lease.lease_id}",
                kind="lease.issued",
                payload={
                    "lease_id": lease.lease_id,
                    "principal_id": lease.principal_id,
                    "continuity_id": lease.continuity_id,
                    "capability": lease.capability,
                    "intent_digest": lease.intent_digest,
                    "scope_digest": lease.scope_digest,
                    "decision_id": lease.decision_id,
                    "approval_id": lease.approval_id,
                    "budget_reservation_id": lease.budget_reservation_id,
                    "obligations": lease.obligations,
                    "expires_at": lease.expires_at,
                },
            )
        except Exception:
            self._rollback_failed_authorization(evaluation, now=now)
            raise
        return AuthorizationResult(
            intent=intent,
            decision=evaluation.decision,
            lease=lease,
            token=token,
        )

    def verify_effect(
        self,
        token: str,
        intent: ActionIntent,
        *,
        evidence: Mapping[str, Any],
        principal_id: str | None = None,
        decision_id: str | None = None,
        now: datetime | None = None,
    ) -> CapabilityLease:
        if self.leases is None:
            raise LeaseError("no lease signing authority is configured")
        lease = self.leases.verify(
            token,
            intent,
            principal_id=principal_id,
            decision_id=decision_id,
            now=now,
        )
        self.obligations.verify(
            lease.obligations,
            evidence,
            phase=ObligationPhase.PRE_EFFECT,
        )
        self.ledger.reserve_lease_use(lease, now=now)
        return lease

    def complete_effect(
        self,
        token: str,
        intent: ActionIntent,
        *,
        success: bool,
        evidence: Mapping[str, Any],
        actual_tokens: int = 0,
        actual_cost_usd: float = 0.0,
        decision: PolicyDecision | None = None,
        now: datetime | None = None,
    ) -> CompletionResult:
        if self.leases is None:
            raise LeaseError("no lease signing authority is configured")
        lease = self.leases.verify(
            token,
            intent,
            decision_id=decision.decision_id if decision else None,
            now=now,
        )
        if decision is not None:
            if (
                decision.outcome is not DecisionOutcome.ALLOW
                or decision.capability != lease.capability
                or decision.intent_digest != lease.intent_digest
                or decision.scope_digest != lease.scope_digest
                or decision.approval_id != lease.approval_id
                or decision.budget_reservation_id != lease.budget_reservation_id
            ):
                raise LeaseTampered(
                    "completion decision does not match signed lease authority"
                )
        self.ledger.require_reserved_lease_use(lease)
        internally_completed = {"receipt.append", "budget.settle"}
        post_without_receipt = tuple(
            item for item in lease.obligations if item not in internally_completed
        )
        self.obligations.verify(
            post_without_receipt,
            evidence,
            phase=ObligationPhase.POST_EFFECT,
        )

        budget_state: str | None = None
        reservation_id = lease.budget_reservation_id
        if "budget.settle" in lease.obligations and reservation_id is None:
            raise LeaseTampered(
                "signed lease requires budget settlement without a reservation"
            )
        if reservation_id:
            if actual_tokens or actual_cost_usd:
                budget_state = self.ledger.settle_budget(
                    reservation_id,
                    actual_tokens=actual_tokens,
                    actual_cost_usd=actual_cost_usd,
                    now=now,
                ).state
            else:
                budget_state = self.ledger.release_budget(
                    reservation_id, now=now
                ).state

        receipt = self.ledger.append_receipt(
            event_id=f"lease-completion:{lease.lease_id}",
            kind="action.completed" if success else "action.failed",
            payload={
                "lease_id": lease.lease_id,
                "decision_id": lease.decision_id,
                "principal_id": lease.principal_id,
                "continuity_id": lease.continuity_id,
                "capability": lease.capability,
                "intent_digest": lease.intent_digest,
                "scope_digest": lease.scope_digest,
                "success": success,
                "actual_tokens": actual_tokens,
                "actual_cost_usd": actual_cost_usd,
                "budget_state": budget_state,
                "evidence_keys": tuple(sorted(evidence)),
            },
        )
        final_obligations = tuple(
            item for item in ("budget.settle", "receipt.append")
            if item in lease.obligations
        )
        if final_obligations:
            self.obligations.verify(
                final_obligations,
                {
                    "budget_settled": budget_state in {"settled", "released"},
                    "receipt_hash": receipt.receipt_hash,
                },
                phase=ObligationPhase.POST_EFFECT,
            )
        self.ledger.complete_lease_use(
            lease,
            success=success,
            receipt_hash=receipt.receipt_hash,
            now=now,
        )
        return CompletionResult(
            lease=lease, receipt=receipt, budget_state=budget_state
        )

    def grant_approval(
        self,
        *,
        principal_id: str,
        approver_id: str,
        intent_digest: str,
        scope_digest: str,
        ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> Any:
        return self.policy.approvals.grant(
            principal_id=principal_id,
            approver_id=approver_id,
            intent_digest=intent_digest,
            scope_digest=scope_digest,
            ttl_seconds=ttl_seconds,
            now=now,
        )

    def observe_fact(self, fact: Fact) -> Fact:
        return self.facts.observe(fact)
