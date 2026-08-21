"""Fail-closed guard for the legacy updater's deployment-plan boundary.

The current updater still performs a monolithic source/runtime mutation. Until its
individual stages consume deployment-plan authority directly, narrowed plans must
be refused rather than admitted and then silently widened by legacy behavior.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from hermes_cli.deployment_plan import (
    SCHEMA_VERSION,
    DeploymentMutationDenied,
    DeploymentPlan,
    DeploymentPlanInvalid,
    default_hermes_home,
    deployment_plan_path,
    plan_from_mapping,
)

_PLAN_KEYS = frozenset(
    {
        "schema_version",
        "mode",
        "kind",
        "canonical_checkout",
        "allowed_components",
        "allowed_runtime_origins",
        "automatic_local_provisioning",
        "required_postconditions",
        "target_generation",
    }
)


def _resolved_plan_path(env: Mapping[str, str]) -> tuple[Path, bool]:
    hermes_home = default_hermes_home(env)
    explicit = bool(env.get("HERMES_DEPLOYMENT_PLAN", "").strip())
    return deployment_plan_path(hermes_home=hermes_home, env=env), explicit


def validate_update_plan_source(env: Mapping[str, str] | None = None) -> None:
    """Reject missing explicit authorities and unknown persisted plan keys.

    The canonical deployment-plan loader retains compatibility inference when no
    plan is configured. That fallback is not valid when an operator explicitly
    names a plan path, and safety-bearing key typos must not be ignored.
    """

    env = os.environ if env is None else env
    path, explicit = _resolved_plan_path(env)
    if explicit and not path.exists():
        raise DeploymentPlanInvalid(
            f"explicit deployment plan does not exist: {path}",
            remediation=(
                "repair HERMES_DEPLOYMENT_PLAN or remove the explicit setting; "
                "Hermes will not infer mutation authority through a missing file"
            ),
        )
    if not path.exists():
        return

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # load_deployment_plan() owns the canonical parse/read diagnostic.
        return
    if not isinstance(raw, dict):
        return

    unknown = sorted(set(raw) - _PLAN_KEYS)
    if unknown:
        raise DeploymentPlanInvalid(
            "unknown deployment-plan keys: " + ", ".join(unknown),
            remediation=(
                "remove or correct unknown keys; Hermes will not ignore fields "
                "that may have been intended to narrow deployment authority"
            ),
        )


def _full_legacy_envelope(plan: DeploymentPlan) -> DeploymentPlan:
    """Build the mode/kind envelope the monolithic updater can enforce today."""

    if plan.canonical_checkout is None:
        raise DeploymentMutationDenied(
            "deployment plan has no canonical checkout for legacy update admission"
        )
    return plan_from_mapping(
        {
            "schema_version": SCHEMA_VERSION,
            "mode": plan.mode.value,
            "kind": plan.kind.value,
            "canonical_checkout": str(plan.canonical_checkout),
            "automatic_local_provisioning": True,
        },
        hermes_home=plan.hermes_home,
        source="legacy-update-envelope",
        project_root=plan.canonical_checkout,
    )


def enforce_legacy_update_envelope(plan: DeploymentPlan) -> None:
    """Refuse authority narrowing that the current updater cannot stage-gate."""

    if not plan.in_place_mutation_allowed:
        return

    full = _full_legacy_envelope(plan)
    if plan.allowed_components != full.allowed_components:
        raise DeploymentMutationDenied(
            "legacy updater cannot enforce narrowed allowed_components",
            remediation=(
                "use a stage-aware updater or restore the full component envelope "
                "for this deployment mode"
            ),
        )
    if plan.allowed_runtime_origins != full.allowed_runtime_origins:
        raise DeploymentMutationDenied(
            "legacy updater cannot enforce narrowed allowed_runtime_origins",
            remediation=(
                "use a stage-aware updater or restore the full runtime-origin "
                "envelope for this deployment mode"
            ),
        )
    if not plan.automatic_local_provisioning:
        raise DeploymentMutationDenied(
            "legacy updater may provision local runtimes but this plan forbids automatic local provisioning",
            remediation=(
                "use a stage-aware updater or explicitly authorize automatic local "
                "provisioning for this legacy mutation path"
            ),
        )
