"""Shared pre-mutation admission contract for Hermes updates.

Every update surface calls :func:`perform_update` before it acquires the
mutation lock, snapshots state, invokes git/package tooling, or restarts a
runtime.  The function is intentionally admission-only: ``None`` authorizes
the existing updater to continue; ``UpdateRefusal`` is a terminal,
machine-readable refusal that has already been persisted as a receipt.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

IMAGE_MANAGED_UPDATE_REFUSED = "image_managed_update_refused"
UPDATE_REFUSED_EXIT = 2


@dataclass
class UpdateRefusal:
    code: str
    message: str
    update_command: str
    deployment_kind: str
    install_method: str
    surface: str
    requested_target: Optional[str]
    classification_reason: str
    baked_identity: dict[str, Any]
    current_identity: dict[str, Any]
    receipt_path: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _image_refusal_message(plan) -> str:
    provenance = plan.image_provenance or {}
    version = provenance.get("version") or plan.expected_version
    revision = provenance.get("revision")
    identity = ""
    if version:
        identity = f" (baked Hermes {version}"
        if revision:
            identity += f" @ {str(revision)[:12]}"
        identity += ")"
    elif revision:
        identity = f" (baked revision {str(revision)[:12]})"

    integrity_note = ""
    if provenance and not provenance.get("valid", True):
        integrity_note = (
            " The baked provenance marker is invalid; the runtime remains "
            "image-managed and is refusing closed."
        )

    lead = (
        f"This Hermes runtime is image-managed{identity}; in-place update is "
        "refused before any mutation. Pull or select the desired image, then "
        "recreate the runtime through its deployment owner (Docker Compose, "
        "Kubernetes, Hermes Cloud, or equivalent)."
        f"{integrity_note}"
    )
    try:
        from hermes_cli.config import format_docker_update_message

        guidance = format_docker_update_message().strip()
    except Exception:
        guidance = f"Update via: {plan.update_mechanism}"
    return f"{lead}\n\n{guidance}"


def evaluate_update_admission(
    *,
    surface: str,
    requested_target: Optional[str] = None,
    project_root: Optional[Path] = None,
    provenance_path: Optional[Path] = None,
    plan=None,
):
    """Return ``(plan, refusal_or_none)`` using read-only probes only."""

    if plan is None:
        from hermes_cli.update_inventory import collect_runtime_inventory

        plan = collect_runtime_inventory(
            project_root=project_root,
            provenance_path=provenance_path,
            include_runtimes=False,
        )

    if plan.deployment_kind != "image":
        return plan, None

    provenance = plan.image_provenance or {}
    current_identity = {
        "sha": plan.expected_sha,
        "version": plan.expected_version,
    }
    refusal = UpdateRefusal(
        code=IMAGE_MANAGED_UPDATE_REFUSED,
        message=_image_refusal_message(plan),
        update_command=plan.update_mechanism,
        deployment_kind=plan.deployment_kind,
        install_method=plan.install_method,
        surface=surface,
        requested_target=requested_target,
        classification_reason=plan.classification_reason,
        baked_identity={
            "image": provenance.get("image"),
            "version": provenance.get("version"),
            "revision": provenance.get("revision"),
            "manager": provenance.get("manager"),
            "valid": provenance.get("valid", True),
            "error": provenance.get("error"),
        },
        current_identity=current_identity,
    )
    return plan, refusal


def _persist_refusal(plan, refusal: UpdateRefusal) -> Optional[str]:
    """Attach the plan/refusal and finalize a durable receipt; never raises."""

    try:
        import hermes_cli.update_receipt as ur
        from hermes_cli.update_inventory import record_plan_in_receipt

        if not ur.has_active_update_receipt():
            ur.begin_update_receipt(
                surface=refusal.surface,
                requested_target=refusal.requested_target,
            )
        record_plan_in_receipt(plan)
        ur.record_refusal(refusal.to_dict())
        path = ur.finalize_update_receipt(
            "refused",
            stop_reason=refusal.code,
        )
        return str(path) if path is not None else None
    except Exception:
        return None


def perform_update(
    *,
    surface: str,
    requested_target: Optional[str] = None,
    project_root: Optional[Path] = None,
    provenance_path: Optional[Path] = None,
    plan=None,
) -> Optional[UpdateRefusal]:
    """Run the shared admission boundary.

    Returns ``None`` when the existing mutable updater may proceed.  Returns a
    finalized refusal for image-managed runtimes.  No update-check/network
    call participates in this decision.
    """

    plan, refusal = evaluate_update_admission(
        surface=surface,
        requested_target=requested_target,
        project_root=project_root,
        provenance_path=provenance_path,
        plan=plan,
    )
    if refusal is None:
        return None
    refusal.receipt_path = _persist_refusal(plan, refusal)
    return refusal
