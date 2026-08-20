"""Task-local Hermes Tag authority carried through gateway execution."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Iterator

from .errors import LeaseError
from .model import CapabilityLease, PolicyDecision, TurnAdmission


_CURRENT_ADMISSION: ContextVar[TurnAdmission | None] = ContextVar(
    "hermes_tag_admission", default=None
)
_CURRENT_DECISION: ContextVar[PolicyDecision | None] = ContextVar(
    "hermes_tag_decision", default=None
)
_CURRENT_LEASE: ContextVar[CapabilityLease | None] = ContextVar(
    "hermes_tag_lease", default=None
)


@dataclass(frozen=True, slots=True)
class RuntimeAuthority:
    admission: TurnAdmission
    decision: PolicyDecision | None = None
    lease: CapabilityLease | None = None


@contextmanager
def bind_admission(admission: TurnAdmission) -> Iterator[TurnAdmission]:
    token = _CURRENT_ADMISSION.set(admission)
    try:
        yield admission
    finally:
        _CURRENT_ADMISSION.reset(token)


@contextmanager
def bind_decision(decision: PolicyDecision) -> Iterator[PolicyDecision]:
    token = _CURRENT_DECISION.set(decision)
    try:
        yield decision
    finally:
        _CURRENT_DECISION.reset(token)


@contextmanager
def bind_lease(lease: CapabilityLease) -> Iterator[CapabilityLease]:
    token = _CURRENT_LEASE.set(lease)
    try:
        yield lease
    finally:
        _CURRENT_LEASE.reset(token)


@contextmanager
def bind_authority(authority: RuntimeAuthority) -> Iterator[RuntimeAuthority]:
    admission_token = _CURRENT_ADMISSION.set(authority.admission)
    decision_token: Token[PolicyDecision | None] | None = None
    lease_token: Token[CapabilityLease | None] | None = None
    if authority.decision is not None:
        decision_token = _CURRENT_DECISION.set(authority.decision)
    if authority.lease is not None:
        lease_token = _CURRENT_LEASE.set(authority.lease)
    try:
        yield authority
    finally:
        if lease_token is not None:
            _CURRENT_LEASE.reset(lease_token)
        if decision_token is not None:
            _CURRENT_DECISION.reset(decision_token)
        _CURRENT_ADMISSION.reset(admission_token)


def current_admission(*, required: bool = False) -> TurnAdmission | None:
    value = _CURRENT_ADMISSION.get()
    if required and value is None:
        raise LeaseError("no Hermes Tag turn admission is bound")
    return value


def current_decision(*, required: bool = False) -> PolicyDecision | None:
    value = _CURRENT_DECISION.get()
    if required and value is None:
        raise LeaseError("no Hermes Tag policy decision is bound")
    return value


def current_lease(*, required: bool = False) -> CapabilityLease | None:
    value = _CURRENT_LEASE.get()
    if required and value is None:
        raise LeaseError("no Hermes Tag capability lease is bound")
    return value


def capture_authority() -> RuntimeAuthority:
    admission = current_admission(required=True)
    assert admission is not None
    return RuntimeAuthority(
        admission=admission,
        decision=current_decision(),
        lease=current_lease(),
    )


def clear_runtime_context() -> None:
    """Explicitly clear the current context for process lifecycle tests."""
    _CURRENT_ADMISSION.set(None)
    _CURRENT_DECISION.set(None)
    _CURRENT_LEASE.set(None)
