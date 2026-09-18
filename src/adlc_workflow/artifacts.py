"""Profile-selected public artifact layout helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class ArtifactLayoutError(ValueError):
    """Raised when a profile contains an unsafe or incomplete artifact layout."""


class ArtifactLayout:
    """Resolve public artifact paths from a workflow profile.

    Profile paths are workspace-relative. Keeping this resolution in one place
    prevents the core and publication adapters from silently assuming the
    feature workflow's directory names.
    """

    def __init__(self, workspace_root: Path, profile: dict[str, Any]):
        config = profile.get("artifacts")
        if not isinstance(config, dict):
            raise ArtifactLayoutError("profile requires an artifacts mapping")
        self.workspace_root = workspace_root
        self.bundle_root = self._path(config.get("bundle_root", "artifacts"), "bundle_root")
        roots = config.get("roots")
        if not isinstance(roots, dict):
            raise ArtifactLayoutError("profile artifacts requires a roots mapping")
        self.roots = {
            str(name): self._path(value, f"roots.{name}")
            for name, value in roots.items()
        }
        evidence = config.get("evidence_files", {})
        if not isinstance(evidence, dict):
            raise ArtifactLayoutError("profile artifacts evidence_files must be a mapping")
        self.evidence = {str(name): str(value) for name, value in evidence.items()}

    def root(self, name: str) -> Path:
        try:
            return self.roots[name]
        except KeyError as exc:
            raise ArtifactLayoutError(f"profile has no artifact root: {name}") from exc

    def evidence_path(self, name: str) -> Path:
        try:
            relative = self.evidence[name]
        except KeyError as exc:
            raise ArtifactLayoutError(f"profile has no evidence file: {name}") from exc
        return self._path(relative, f"evidence_files.{name}", base=self.bundle_root)

    def task(self, issue_key: str) -> Path:
        return self.root("tasks") / f"{issue_key}.md"

    def review(self, issue_key: str) -> Path:
        return self.root("reviews") / f"{issue_key}-review.md"

    def reviewer_glob(self, issue_key: str) -> str:
        return f"{issue_key}-*-review.md"

    def _path(self, value: Any, field: str, base: Path | None = None) -> Path:
        if not isinstance(value, str) or not value:
            raise ArtifactLayoutError(f"profile requires a non-empty {field}")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ArtifactLayoutError(f"artifact path must stay within workspace: {field}")
        return (base or self.workspace_root) / path
