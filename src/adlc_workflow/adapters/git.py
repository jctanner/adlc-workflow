"""Runtime archive adapter for the first local publication slice."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactLayout


class ArchiveAdapterError(RuntimeError):
    """Raised when the local archive cannot be created or verified."""


class LocalArchivePublisher:
    """Materialize a complete run bundle without requiring a git checkout.

    A future git-backed adapter can push this exact bundle. Keeping the
    materialization and receipt contract here lets local mode exercise the
    publication boundary now.
    """

    def publish(self, workspace_root: Path, run_id: str, items: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
        artifacts = ArtifactLayout(workspace_root, profile)
        archive_root = artifacts.root("published") / run_id
        archive_root.mkdir(parents=True, exist_ok=True)
        manifest_items = []
        for item in items:
            work_id = item["work_id"]
            source_paths = {
                "strategy": artifacts.task(item["key"]),
                "review": artifacts.review(item["key"]),
            }
            copied = {}
            for name, source in source_paths.items():
                if not source.is_file():
                    raise ArchiveAdapterError(f"missing {name} artifact for {item['key']}: {source}")
                destination = archive_root / work_id / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                copied[name] = str(destination.relative_to(workspace_root))
            reviewer_files = []
            review_root = artifacts.root("reviews")
            for source in sorted(review_root.glob(artifacts.reviewer_glob(item["key"]))):
                destination = archive_root / work_id / source.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                reviewer_files.append(str(destination.relative_to(workspace_root)))
            if reviewer_files:
                copied["reviewers"] = reviewer_files
            manifest_items.append({"work_id": work_id, "issue_key": item["key"], "artifacts": copied})

        manifest = {"schema_version": 1, "run_id": run_id, "items": manifest_items}
        manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
        manifest_path = archive_root / "manifest.json"
        manifest_path.write_bytes(manifest_bytes)
        digest = hashlib.sha256(manifest_bytes).hexdigest()
        receipt = {
            "adapter": "local-archive",
            "run_id": run_id,
            "status": "published",
            "root": str(archive_root.relative_to(workspace_root)),
            "manifest": str(manifest_path.relative_to(workspace_root)),
            "manifest_sha256": digest,
        }
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            raise ArchiveAdapterError("local archive manifest verification failed")
        return receipt
