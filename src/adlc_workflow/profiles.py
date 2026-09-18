"""Reviewed lifecycle-profile and artifact-layout loading."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class ProfileError(ValueError):
    """Raised when a profile is missing or invalid."""


def load_profile(project_root: str | Path, profile_path: str) -> dict[str, Any]:
    # Do not resolve symlinks before the containment check. The Claude local
    # marketplace intentionally links cached plugin files back to the mounted
    # install, and those links are still part of the selected install root.
    root = Path(project_root).absolute()
    path = root / profile_path
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ProfileError("profile path must stay within project root") from exc
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"cannot load profile {profile_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileError("profile must contain a mapping")
    for key in ("api_version", "id", "lifecycle"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ProfileError(f"profile requires non-empty {key}")
    return value
