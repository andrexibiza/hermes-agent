#!/usr/bin/env python3
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def write(path: str, content: str) -> None:
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def replace_once(path: str, old: str, new: str) -> None:
    text = read(path)
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{path}: expected one literal match, found {count}")
    write(path, text.replace(old, new, 1))


def sub_once(path: str, pattern: str, replacement: str) -> None:
    text = read(path)
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.DOTALL)
    if count != 1:
        raise RuntimeError(f"{path}: expected one regex match, found {count}")
    write(path, new)


INVENTORY_INSTALL_BLOCK = '    # --- install shape / deployment kind ---------------------------------\n    try:\n        from hermes_cli.config import (\n            detect_install_method,\n            get_managed_system,\n            recommended_update_command_for_method,\n        )\n        from hermes_cli.image_provenance import read_image_provenance\n\n        provenance = read_image_provenance(provenance_path)\n        if provenance is not None:\n            # Image-authored provenance outranks every mutable checkout hint,\n            # including a bind-mounted repository with its own .git directory.\n            plan.install_method = "docker"\n            plan.deployment_kind = "image"\n            plan.classification_reason = (\n                "baked_image_provenance"\n                if provenance.valid\n                else "invalid_baked_image_provenance"\n            )\n            plan.image_provenance = provenance.to_dict()\n            plan.updatable_in_place = False\n            plan.update_mechanism = recommended_update_command_for_method("docker")\n        else:\n            method = detect_install_method(project_root)\n            plan.install_method = method\n            managed = get_managed_system()\n            if managed:\n                plan.install_method = managed\n                plan.deployment_kind = "package"\n                plan.classification_reason = f"managed_system:{managed}"\n            elif method == "docker":\n                # Backward compatibility for images built before the explicit\n                # provenance marker landed. They still refuse in place.\n                plan.deployment_kind = "image"\n                plan.classification_reason = "install_method:docker"\n            elif method in ("nix", "nixos", "home-manager", "apt"):\n                plan.deployment_kind = "package"\n                plan.classification_reason = f"install_method:{method}"\n            else:\n                plan.deployment_kind = "mutable"\n                plan.classification_reason = f"install_method:{method}"\n            plan.updatable_in_place = (\n                plan.deployment_kind == "mutable"\n                and method in ("git", "unknown")\n                and not managed\n            )\n            plan.update_mechanism = recommended_update_command_for_method(method)\n    except Exception as exc:\n        logger.debug("Install-method probe failed: %s", exc)\n'
replace_once(
    "hermes_cli/update_inventory.py",
    '    install_method: str = "unknown"       # git | docker | nix | apt | ...\n'
    '    updatable_in_place: bool = True\n'
    '    update_mechanism: str = "hermes update"\n',
    '    install_method: str = "unknown"       # git | docker | nix | apt | ...\n'
    '    deployment_kind: str = "mutable"      # mutable | package | image\n'
    '    classification_reason: str = ""\n'
    '    image_provenance: Optional[dict] = None\n'
    '    updatable_in_place: bool = True\n'
    '    update_mechanism: str = "hermes update"\n',
)
replace_once(
    "hermes_cli/update_inventory.py",
    'def collect_runtime_inventory() -> UpdatePlan:\n',
    'def collect_runtime_inventory(\n'
    '    project_root: Optional[Path] = None,\n'
    '    *,\n'
    '    provenance_path: Optional[Path] = None,\n'
    '    include_runtimes: bool = True,\n'
    ') -> UpdatePlan:\n',
)
sub_once(
    "hermes_cli/update_inventory.py",
    r'    # --- install shape / deployment kind -+\n'
    r'    try:\n.*?'
    r'    except Exception as exc:\n'
    r'        logger\.debug\("Install-method probe failed: %s", exc\)\n',
    INVENTORY_INSTALL_BLOCK,
)
replace_once(
    "hermes_cli/update_inventory.py",
    '    except Exception as exc:\n'
    '        logger.debug("Code-identity probe failed: %s", exc)\n\n'
    '    # --- profiles ----------------------------------------------------------\n',
    '    except Exception as exc:\n'
    '        logger.debug("Code-identity probe failed: %s", exc)\n\n'
    '    if not include_runtimes:\n'
    '        return plan\n\n'
    '    # --- profiles ----------------------------------------------------------\n',
)
replace_once(
    "hermes_cli/update_inventory.py",
    '        print(")", end="")\n'
    '    print()\n'
    '    if not plan.updatable_in_place:\n',
    '        print(")", end="")\n'
    '    print()\n'
    '    reason = f" ({plan.classification_reason})" if plan.classification_reason else ""\n'
    '    print(f"  Deployment: {plan.deployment_kind}{reason}")\n'
    '    if plan.image_provenance:\n'
    '        marker = plan.image_provenance.get("marker_path")\n'
    '        revision = plan.image_provenance.get("revision")\n'
    '        identity = f" @ {str(revision)[:12]}" if revision else ""\n'
    '        print(f"  Image provenance: {marker}{identity}")\n'
    '    if not plan.updatable_in_place:\n',
)

replace_once(
    "hermes_cli/update_receipt.py",
    'import time\n'
    'from datetime import datetime, timezone\n',
    'import time\n'
    'import uuid\n'
    'from datetime import datetime, timezone\n',
)
replace_once(
    "hermes_cli/update_receipt.py",
    'def _utc_now_iso() -> str:\n'
    '    return datetime.now(timezone.utc).isoformat()\n\n\n'
    'class UpdateReceipt:\n',
    'def _utc_now_iso() -> str:\n'
    '    return datetime.now(timezone.utc).isoformat()\n\n\n'
    'def _correlation_id() -> str:\n'
    '    action_id = os.environ.get("HERMES_ACTION_ID", "")\n'
    '    if len(action_id) == 32 and all(c in "0123456789abcdef" for c in action_id):\n'
    '        return action_id\n'
    '    return uuid.uuid4().hex\n\n\n'
    'class UpdateReceipt:\n',
)
replace_once(
    "hermes_cli/update_receipt.py",
    '    def __init__(self) -> None:\n'
    '        self.data: dict[str, Any] = {\n'
    '            "schema": 1,\n'
    '            "started_at": _utc_now_iso(),\n'
    '            "finished_at": None,\n'
    '            "argv": list(sys.argv),\n'
    '            "pid": os.getpid(),\n'
    '            "outcome": "running",  # running | success | partial | failed\n',
    '    def __init__(\n'
    '        self, surface: str = "", requested_target: Optional[str] = None\n'
    '    ) -> None:\n'
    '        self.data: dict[str, Any] = {\n'
    '            "schema": 1,\n'
    '            "correlation_id": _correlation_id(),\n'
    '            "started_at": _utc_now_iso(),\n'
    '            "finished_at": None,\n'
    '            "argv": list(sys.argv),\n'
    '            "pid": os.getpid(),\n'
    '            "surface": surface,\n'
    '            "requested_target": requested_target,\n'
    '            "refusal": {},\n'
    '            "outcome": "running",  # running | success | partial | failed | refused\n',
)
replace_once(
    "hermes_cli/update_receipt.py",
    'def begin_update_receipt() -> None:\n'
    '    """Start recording a new update receipt. Never raises."""\n'
    '    global _current\n'
    '    try:\n'
    '        _current = UpdateReceipt()\n',
    'def begin_update_receipt(\n'
    '    surface: str = "", requested_target: Optional[str] = None\n'
    ') -> None:\n'
    '    """Start recording a new update receipt. Never raises."""\n'
    '    global _current\n'
    '    try:\n'
    '        _current = UpdateReceipt(surface, requested_target)\n',
)
replace_once(
    "hermes_cli/update_receipt.py",
    '        _current = None\n\n\n'
    'def record_step(name: str, ok: bool, detail: str = "") -> None:\n',
    '        _current = None\n\n\n'
    'def has_active_update_receipt() -> bool:\n'
    '    """Whether a receipt is currently collecting in this process."""\n'
    '    return _current is not None\n\n\n'
    'def record_step(name: str, ok: bool, detail: str = "") -> None:\n',
)
replace_once(
    "hermes_cli/update_receipt.py",
    'def record_gateway_restart(**kwargs: Any) -> None:\n',
    'def record_refusal(payload: dict[str, Any]) -> None:\n'
    '    """Record one typed terminal refusal on the active receipt."""\n'
    '    try:\n'
    '        if _current is None:\n'
    '            return\n'
    '        clean = dict(payload)\n'
    '        _current.data["refusal"] = clean\n'
    '        if clean.get("surface"):\n'
    '            _current.data["surface"] = clean["surface"]\n'
    '        if "requested_target" in clean:\n'
    '            _current.data["requested_target"] = clean["requested_target"]\n'
    '    except Exception as exc:  # pragma: no cover - defensive\n'
    '        logger.debug("Could not record update refusal: %s", exc)\n\n\n'
    'def record_gateway_restart(**kwargs: Any) -> None:\n',
)


print("phase3 inventory/receipt patch complete")
