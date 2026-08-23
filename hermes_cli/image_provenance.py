"""Image-authored deployment provenance for immutable Hermes runtimes.

The published image bakes ``/etc/hermes/image-provenance.json`` outside both
``$HERMES_HOME`` and the mutable checkout.  A bind-mounted checkout (including
``.git``) therefore cannot hide the build fact, and environment/config values
cannot forge it.

Absence preserves every pre-existing source/package install path.  Presence
fails closed: an unreadable or malformed image-authored marker still means the
runtime is image-managed; it is an integrity defect, never permission to
mutate the image in place.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

IMAGE_PROVENANCE_PATH = Path("/etc/hermes/image-provenance.json")
IMAGE_PROVENANCE_SCHEMA = 1


@dataclass(frozen=True)
class ImageProvenance:
    schema: int
    deployment_kind: str
    manager: str
    image: Optional[str]
    version: Optional[str]
    revision: Optional[str]
    marker_path: str
    valid: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _invalid(path: Path, reason: str) -> ImageProvenance:
    return ImageProvenance(
        schema=IMAGE_PROVENANCE_SCHEMA,
        deployment_kind="image",
        manager="unknown",
        image=None,
        version=None,
        revision=None,
        marker_path=str(path),
        valid=False,
        error=reason,
    )


def read_image_provenance(
    marker_path: Optional[Path] = None,
) -> Optional[ImageProvenance]:
    """Read the baked marker without consulting env/config; never raises.

    ``None`` means no image marker exists.  Any present-but-invalid marker
    returns an invalid ``ImageProvenance`` so callers fail closed.
    ``marker_path`` exists only for deterministic tests and alternate image
    builders; normal runtime callers use the image-owned absolute path.
    """

    path = Path(marker_path) if marker_path is not None else IMAGE_PROVENANCE_PATH
    try:
        exists = path.exists()
    except OSError as exc:
        # We cannot prove absence when the image-owned path is unreadable.
        return _invalid(path, f"marker_presence_unreadable:{type(exc).__name__}")
    if not exists:
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return _invalid(path, f"marker_unreadable:{type(exc).__name__}")

    if not isinstance(payload, dict):
        return _invalid(path, "marker_not_object")
    if payload.get("schema") != IMAGE_PROVENANCE_SCHEMA:
        return _invalid(path, "unsupported_marker_schema")
    if payload.get("deployment_kind") != "image":
        return _invalid(path, "invalid_deployment_kind")

    manager = payload.get("manager")
    if not isinstance(manager, str) or not manager.strip():
        return _invalid(path, "missing_manager")

    def _optional_string(name: str) -> Optional[str]:
        value = payload.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError(name)
        value = value.strip()
        return value or None

    try:
        image = _optional_string("image")
        version = _optional_string("version")
        revision = _optional_string("revision")
    except TypeError as exc:
        return _invalid(path, f"invalid_{exc.args[0]}")

    return ImageProvenance(
        schema=IMAGE_PROVENANCE_SCHEMA,
        deployment_kind="image",
        manager=manager.strip(),
        image=image,
        version=version,
        revision=revision,
        marker_path=str(path),
    )
