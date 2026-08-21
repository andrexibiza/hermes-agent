"""Authoritative deployment-plan model for install/update/bootstrap admission.

This module is deliberately import-light and side-effect-free. It answers what
may exist or mutate on the current machine; callers own the actual mutation.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, MutableMapping

SCHEMA_VERSION = 1
DEFAULT_PLAN_FILE = "deployment-plan.json"


class DeploymentPlanError(RuntimeError):
    """Base error for invalid or unenforceable deployment plans."""

    code = "DEPLOYMENT_PLAN_ERROR"

    def __init__(self, message: str, *, remediation: str | None = None) -> None:
        super().__init__(message)
        self.remediation = remediation


class DeploymentPlanInvalid(DeploymentPlanError):
    """A persisted or explicit plan is malformed or internally inconsistent."""

    code = "DEPLOYMENT_PLAN_INVALID"


class DeploymentMutationDenied(DeploymentPlanError):
    """The requested mutation is outside the authoritative deployment plan."""

    code = "DEPLOYMENT_MUTATION_DENIED"


class DeploymentMode(str, Enum):
    CLI_ONLY = "cli-only"
    LOCAL_DESKTOP = "local-desktop"
    REMOTE_ONLY = "remote-only"
    HYBRID = "hybrid"


class DeploymentKind(str, Enum):
    GIT_VENV = "git-venv"
    LAUNCHD = "launchd-supervised"
    SYSTEMD = "systemd-supervised"
    DESKTOP_MANAGED = "desktop-managed"
    EXTERNALLY_MANAGED = "externally-managed"
    IMAGE_MANAGED = "image-managed"


_MODE_COMPONENT_CEILINGS: Mapping[DeploymentMode, frozenset[str]] = MappingProxyType(
    {
        DeploymentMode.CLI_ONLY: frozenset({"source", "python-runtime", "cli"}),
        DeploymentMode.LOCAL_DESKTOP: frozenset(
            {
                "source",
                "python-runtime",
                "node-runtime",
                "cli",
                "desktop",
                "gateway",
            }
        ),
        DeploymentMode.REMOTE_ONLY: frozenset({"desktop-client", "remote-routes"}),
        DeploymentMode.HYBRID: frozenset(
            {
                "source",
                "python-runtime",
                "node-runtime",
                "cli",
                "desktop",
                "gateway",
                "desktop-client",
                "remote-routes",
            }
        ),
    }
)

_MODE_RUNTIME_ORIGINS: Mapping[DeploymentMode, frozenset[str]] = MappingProxyType(
    {
        DeploymentMode.CLI_ONLY: frozenset({"managed", "external"}),
        DeploymentMode.LOCAL_DESKTOP: frozenset({"managed", "external"}),
        DeploymentMode.REMOTE_ONLY: frozenset({"remote"}),
        DeploymentMode.HYBRID: frozenset({"managed", "external", "remote"}),
    }
)

_MUTABLE_KINDS = frozenset(
    {
        DeploymentKind.GIT_VENV,
        DeploymentKind.LAUNCHD,
        DeploymentKind.SYSTEMD,
        DeploymentKind.DESKTOP_MANAGED,
    }
)


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    """Immutable authority describing the deployment that may exist."""

    schema_version: int
    mode: DeploymentMode
    kind: DeploymentKind
    hermes_home: Path
    canonical_checkout: Path | None
    allowed_components: frozenset[str]
    allowed_runtime_origins: frozenset[str]
    automatic_local_provisioning: bool
    required_postconditions: tuple[str, ...]
    source: str
    target_generation: str | None = None

    @property
    def in_place_mutation_allowed(self) -> bool:
        return self.kind in _MUTABLE_KINDS and self.mode is not DeploymentMode.REMOTE_ONLY

    @property
    def digest(self) -> str:
        payload = self.to_dict(include_source=False)
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self, *, include_source: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "mode": self.mode.value,
            "kind": self.kind.value,
            "hermes_home": str(self.hermes_home),
            "canonical_checkout": (
                str(self.canonical_checkout)
                if self.canonical_checkout is not None
                else None
            ),
            "allowed_components": sorted(self.allowed_components),
            "allowed_runtime_origins": sorted(self.allowed_runtime_origins),
            "automatic_local_provisioning": self.automatic_local_provisioning,
            "required_postconditions": list(self.required_postconditions),
            "target_generation": self.target_generation,
        }
        if include_source:
            payload["source"] = self.source
            payload["digest"] = self.digest
        return payload

    def assert_component_allowed(self, component: str) -> None:
        if component not in self.allowed_components:
            raise DeploymentMutationDenied(
                f"deployment mode {self.mode.value!r} forbids component {component!r}",
                remediation=(
                    "change the persisted deployment plan explicitly before "
                    "adding or activating this component"
                ),
            )

    def assert_runtime_origin_allowed(self, origin: str) -> None:
        if origin not in self.allowed_runtime_origins:
            raise DeploymentMutationDenied(
                f"deployment mode {self.mode.value!r} forbids runtime origin {origin!r}",
                remediation="select a runtime origin authorized by the deployment plan",
            )


def _resolved_path(value: str | os.PathLike[str]) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _env_flag(env: Mapping[str, str], name: str) -> bool:
    return env.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def default_hermes_home(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    configured = env.get("HERMES_HOME")
    if configured:
        return _resolved_path(configured)
    return _resolved_path(Path.home() / ".hermes")


def default_project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def deployment_plan_path(
    *,
    hermes_home: Path,
    env: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if env is None else env
    explicit = env.get("HERMES_DEPLOYMENT_PLAN")
    if explicit:
        return _resolved_path(explicit)
    return hermes_home / DEFAULT_PLAN_FILE


def _enum_value(enum_type, raw: Any, field: str):
    try:
        return enum_type(str(raw))
    except (TypeError, ValueError) as exc:
        choices = ", ".join(member.value for member in enum_type)
        raise DeploymentPlanInvalid(
            f"invalid deployment-plan {field}: {raw!r}; expected one of {choices}"
        ) from exc


def _string_set(raw: Any, field: str) -> frozenset[str]:
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise DeploymentPlanInvalid(
            f"deployment-plan {field} must be a list of non-empty strings"
        )
    return frozenset(item.strip() for item in raw)


def _string_tuple(raw: Any, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item.strip() for item in raw
    ):
        raise DeploymentPlanInvalid(
            f"deployment-plan {field} must be a list of non-empty strings"
        )
    return tuple(item.strip() for item in raw)


def _default_postconditions(mode: DeploymentMode) -> tuple[str, ...]:
    common = ("source-generation", "python-generation")
    if mode is DeploymentMode.CLI_ONLY:
        return common + ("cli-entrypoint",)
    if mode is DeploymentMode.LOCAL_DESKTOP:
        return common + (
            "node-generation",
            "desktop-generation",
            "managed-process-generation",
        )
    if mode is DeploymentMode.REMOTE_ONLY:
        return ("no-local-runtime", "desktop-client-generation")
    return common + (
        "node-generation",
        "desktop-generation",
        "managed-process-generation",
        "remote-route-continuity",
    )


def _validate_plan(
    *,
    mode: DeploymentMode,
    kind: DeploymentKind,
    allowed_components: frozenset[str],
    allowed_runtime_origins: frozenset[str],
    automatic_local_provisioning: bool,
    canonical_checkout: Path | None,
) -> None:
    component_ceiling = _MODE_COMPONENT_CEILINGS[mode]
    excess = allowed_components - component_ceiling
    if excess:
        raise DeploymentPlanInvalid(
            f"deployment mode {mode.value!r} cannot authorize components: "
            + ", ".join(sorted(excess))
        )

    origin_ceiling = _MODE_RUNTIME_ORIGINS[mode]
    excess_origins = allowed_runtime_origins - origin_ceiling
    if excess_origins:
        raise DeploymentPlanInvalid(
            f"deployment mode {mode.value!r} cannot authorize runtime origins: "
            + ", ".join(sorted(excess_origins))
        )

    if mode is DeploymentMode.REMOTE_ONLY and automatic_local_provisioning:
        raise DeploymentPlanInvalid(
            "remote-only deployments cannot authorize automatic local provisioning"
        )
    if kind in {DeploymentKind.IMAGE_MANAGED, DeploymentKind.EXTERNALLY_MANAGED}:
        if automatic_local_provisioning:
            raise DeploymentPlanInvalid(
                f"{kind.value} deployments cannot authorize local provisioning"
            )
    if kind in _MUTABLE_KINDS and canonical_checkout is None:
        raise DeploymentPlanInvalid(
            f"{kind.value} deployments require a canonical_checkout"
        )


def plan_from_mapping(
    raw: Mapping[str, Any],
    *,
    hermes_home: Path,
    source: str,
    project_root: Path | None = None,
) -> DeploymentPlan:
    if raw.get("schema_version") != SCHEMA_VERSION:
        raise DeploymentPlanInvalid(
            f"unsupported deployment-plan schema_version: "
            f"{raw.get('schema_version')!r}; expected {SCHEMA_VERSION}"
        )

    mode = _enum_value(DeploymentMode, raw.get("mode"), "mode")
    kind = _enum_value(DeploymentKind, raw.get("kind"), "kind")
    project_root = default_project_root() if project_root is None else project_root

    if "allowed_components" in raw:
        allowed_components = _string_set(raw["allowed_components"], "allowed_components")
    else:
        allowed_components = _MODE_COMPONENT_CEILINGS[mode]

    if "allowed_runtime_origins" in raw:
        allowed_runtime_origins = _string_set(
            raw["allowed_runtime_origins"], "allowed_runtime_origins"
        )
    else:
        allowed_runtime_origins = _MODE_RUNTIME_ORIGINS[mode]

    automatic_local_provisioning = raw.get(
        "automatic_local_provisioning",
        mode
        in {
            DeploymentMode.CLI_ONLY,
            DeploymentMode.LOCAL_DESKTOP,
            DeploymentMode.HYBRID,
        }
        and kind in _MUTABLE_KINDS,
    )
    if not isinstance(automatic_local_provisioning, bool):
        raise DeploymentPlanInvalid(
            "deployment-plan automatic_local_provisioning must be boolean"
        )

    checkout_raw = raw.get("canonical_checkout")
    if checkout_raw is None:
        canonical_checkout = None
    elif isinstance(checkout_raw, str) and checkout_raw.strip():
        canonical_checkout = _resolved_path(checkout_raw)
    else:
        raise DeploymentPlanInvalid(
            "deployment-plan canonical_checkout must be a non-empty path or null"
        )

    if "required_postconditions" in raw:
        required_postconditions = _string_tuple(
            raw["required_postconditions"], "required_postconditions"
        )
    else:
        required_postconditions = _default_postconditions(mode)

    target_generation = raw.get("target_generation")
    if target_generation is not None and (
        not isinstance(target_generation, str) or not target_generation.strip()
    ):
        raise DeploymentPlanInvalid(
            "deployment-plan target_generation must be a non-empty string or null"
        )
    if isinstance(target_generation, str):
        target_generation = target_generation.strip()

    _validate_plan(
        mode=mode,
        kind=kind,
        allowed_components=allowed_components,
        allowed_runtime_origins=allowed_runtime_origins,
        automatic_local_provisioning=automatic_local_provisioning,
        canonical_checkout=canonical_checkout,
    )

    return DeploymentPlan(
        schema_version=SCHEMA_VERSION,
        mode=mode,
        kind=kind,
        hermes_home=hermes_home,
        canonical_checkout=canonical_checkout,
        allowed_components=allowed_components,
        allowed_runtime_origins=allowed_runtime_origins,
        automatic_local_provisioning=automatic_local_provisioning,
        required_postconditions=required_postconditions,
        source=source,
        target_generation=target_generation,
    )


def infer_compatibility_plan(
    *,
    hermes_home: Path,
    project_root: Path,
    env: Mapping[str, str],
) -> DeploymentPlan:
    explicit_kind = env.get("HERMES_DEPLOYMENT_KIND")
    if explicit_kind:
        kind = _enum_value(DeploymentKind, explicit_kind.strip(), "kind")
    elif _env_flag(env, "HERMES_IMAGE_MANAGED"):
        kind = DeploymentKind.IMAGE_MANAGED
    elif _env_flag(env, "HERMES_DESKTOP"):
        kind = DeploymentKind.DESKTOP_MANAGED
    else:
        kind = DeploymentKind.GIT_VENV

    explicit_mode = env.get("HERMES_DEPLOYMENT_MODE")
    if explicit_mode:
        mode = _enum_value(DeploymentMode, explicit_mode.strip(), "mode")
    elif _env_flag(env, "HERMES_REMOTE_ONLY"):
        mode = DeploymentMode.REMOTE_ONLY
    elif kind is DeploymentKind.DESKTOP_MANAGED:
        mode = DeploymentMode.LOCAL_DESKTOP
    else:
        mode = DeploymentMode.CLI_ONLY

    canonical_checkout = (
        None
        if kind in {DeploymentKind.IMAGE_MANAGED, DeploymentKind.EXTERNALLY_MANAGED}
        else project_root
    )
    auto_provision = mode is not DeploymentMode.REMOTE_ONLY and kind in _MUTABLE_KINDS
    return plan_from_mapping(
        {
            "schema_version": SCHEMA_VERSION,
            "mode": mode.value,
            "kind": kind.value,
            "canonical_checkout": (
                str(canonical_checkout) if canonical_checkout is not None else None
            ),
            "automatic_local_provisioning": auto_provision,
        },
        hermes_home=hermes_home,
        source="compatibility-inference",
        project_root=project_root,
    )


def load_deployment_plan(
    *,
    env: Mapping[str, str] | None = None,
    project_root: Path | None = None,
    hermes_home: Path | None = None,
) -> DeploymentPlan:
    env = os.environ if env is None else env
    project_root = (
        default_project_root() if project_root is None else _resolved_path(project_root)
    )
    hermes_home = (
        default_hermes_home(env)
        if hermes_home is None
        else _resolved_path(hermes_home)
    )
    path = deployment_plan_path(hermes_home=hermes_home, env=env)
    if not path.exists():
        return infer_compatibility_plan(
            hermes_home=hermes_home,
            project_root=project_root,
            env=env,
        )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentPlanInvalid(
            f"cannot read deployment plan {path}: {exc}",
            remediation=(
                f"repair or remove {path}; Hermes will not guess through an "
                "explicit but unreadable deployment authority"
            ),
        ) from exc
    if not isinstance(raw, dict):
        raise DeploymentPlanInvalid(
            f"deployment plan {path} must contain a JSON object"
        )
    return plan_from_mapping(
        raw,
        hermes_home=hermes_home,
        source=str(path),
        project_root=project_root,
    )


def _mutation_remediation(plan: DeploymentPlan) -> str:
    if plan.kind is DeploymentKind.IMAGE_MANAGED:
        return "pull the target Hermes image and recreate the container"
    if plan.kind is DeploymentKind.EXTERNALLY_MANAGED:
        return "use the external package or deployment manager that owns this install"
    if plan.mode is DeploymentMode.REMOTE_ONLY:
        return "update the remote backend or packaged client through its declared owner"
    return "change the persisted deployment plan explicitly before mutating this install"


def admit_update(
    args: Any,
    *,
    env: MutableMapping[str, str] | None = None,
    project_root: Path | None = None,
) -> DeploymentPlan:
    """Admit one update command and attach its immutable plan to ``args``.

    ``--check`` is observation and remains side-effect-free for every deployment
    kind. Mutation requires an in-place-mutable kind, a non-remote-only mode,
    and execution from the canonical checkout.
    """

    env = os.environ if env is None else env
    project_root = (
        default_project_root() if project_root is None else _resolved_path(project_root)
    )
    plan = load_deployment_plan(env=env, project_root=project_root)
    setattr(args, "_deployment_plan", plan)

    if bool(getattr(args, "check", False)):
        return plan

    if not plan.in_place_mutation_allowed:
        raise DeploymentMutationDenied(
            f"{plan.kind.value}/{plan.mode.value} is not updatable in place",
            remediation=_mutation_remediation(plan),
        )

    if plan.canonical_checkout is None:
        raise DeploymentMutationDenied(
            "deployment plan has no canonical checkout for in-place update",
            remediation=_mutation_remediation(plan),
        )
    if _resolved_path(plan.canonical_checkout) != project_root:
        raise DeploymentMutationDenied(
            "this process is not running from the deployment plan's canonical "
            f"checkout ({plan.canonical_checkout})",
            remediation=f"run `hermes update` from {plan.canonical_checkout}",
        )

    env["HERMES_DEPLOYMENT_PLAN_DIGEST"] = plan.digest
    return plan
