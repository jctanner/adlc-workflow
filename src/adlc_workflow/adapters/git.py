"""Runtime archive adapter for the first local publication slice."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..artifacts import ArtifactLayout
from ..bundles import generate_bundle, manifest_digest


class ArchiveAdapterError(RuntimeError):
    """Raised when the local archive cannot be created or verified."""


class LocalArchivePublisher:
    """Materialize a complete run bundle without requiring a git checkout.

    A future git-backed adapter can push this exact bundle. Keeping the
    materialization and receipt contract here lets local mode exercise the
    publication boundary now.
    """

    def publish(
        self,
        workspace_root: Path,
        run_id: str,
        items: list[dict[str, Any]],
        profile: dict[str, Any],
        mode: str = "local",
        trusted: bool = False,
    ) -> dict[str, Any]:
        artifacts = ArtifactLayout(workspace_root, profile)
        archive_root = artifacts.root("published") / run_id
        try:
            manifest = generate_bundle(workspace_root, archive_root, run_id, items, profile, mode, trusted)
        except (OSError, ValueError, RuntimeError) as exc:
            raise ArchiveAdapterError(str(exc)) from exc
        manifest_path = archive_root / "manifest.json"
        digest = manifest_digest(manifest)
        receipt = {
            "adapter": "local-archive",
            "run_id": run_id,
            "status": "published",
            "root": str(archive_root.relative_to(workspace_root)),
            "manifest": str(manifest_path.relative_to(workspace_root)),
            "manifest_sha256": digest,
        }
        return receipt
