"""Generate and validate the portable workflow result bundle."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

from .admission import admit_bundle, load_policy
from .artifacts import ArtifactLayout


class BundleError(RuntimeError):
    """Raised when a workflow bundle is incomplete or malformed."""


def generate_bundle(
    workspace_root: Path,
    archive_root: Path,
    run_id: str,
    items: list[dict[str, Any]],
    profile: dict[str, Any],
    mode: str = "local",
    trusted: bool = False,
) -> dict[str, Any]:
    """Copy the accepted public artifacts into a self-contained run bundle."""
    if not isinstance(run_id, str) or not run_id:
        raise BundleError("bundle requires a non-empty run_id")
    if not isinstance(items, list):
        raise BundleError("bundle items must be a list")

    workspace_root = workspace_root.resolve()
    archive_root = archive_root.resolve()
    artifacts = ArtifactLayout(workspace_root, profile)
    archive_root.mkdir(parents=True, exist_ok=True)
    manifest_items: list[dict[str, Any]] = []
    seen_work_ids: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            raise BundleError("bundle items must be objects")
        work_id = item.get("work_id")
        issue_key = item.get("key", item.get("issue_key"))
        if not isinstance(work_id, str) or not work_id:
            raise BundleError("bundle item requires a non-empty work_id")
        if work_id in seen_work_ids:
            raise BundleError(f"duplicate bundle work_id: {work_id}")
        if not isinstance(issue_key, str) or not issue_key:
            raise BundleError(f"bundle item {work_id} requires an issue key")
        seen_work_ids.add(work_id)

        source_paths: dict[str, Path] = {
            "strategy": artifacts.task(issue_key),
            "review": artifacts.review(issue_key),
        }
        context_manifest = artifacts.root("inputs") / "context-manifest.json"
        if context_manifest.is_file():
            source_paths["context"] = context_manifest
        for reviewer_id, source in artifacts.reviewer_paths(issue_key).items():
            source_paths[f"reviewer:{reviewer_id}"] = source
        bundle_config = profile.get("artifacts", {}).get("bundle", {})
        if isinstance(bundle_config, dict):
            generated = bundle_config.get("generated", [])
            if isinstance(generated, list):
                for name in generated:
                    if not isinstance(name, str):
                        raise BundleError(f"invalid generated bundle artifact for {issue_key}")
                    source_paths[f"generated:{name}"] = artifacts.generated(name, issue_key)
            roots = bundle_config.get("roots", [])
            if isinstance(roots, list):
                for root_name in roots:
                    root = artifacts.root(root_name)
                    for source in sorted(root.glob(f"{issue_key}-*")):
                        if source.is_file():
                            source_paths[f"{root_name}:{source.name}"] = source

        copied: dict[str, str] = {}
        destination_names: set[str] = set()
        item_root = archive_root / work_id
        for name, source in source_paths.items():
            if not source.is_file() or not source.read_text(encoding="utf-8").strip():
                raise BundleError(f"missing or empty {name} artifact for {issue_key}: {source}")
            destination = item_root / source.name
            if destination.name == "manifest.json" or destination.name in destination_names:
                raise BundleError(f"artifact filename collides in bundle item {work_id}: {destination.name}")
            destination_names.add(destination.name)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            copied[name] = str(destination.relative_to(archive_root))
        manifest_items.append({"work_id": work_id, "issue_key": issue_key, "artifacts": copied})

    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "mode": mode,
        "trusted": trusted,
        "items": manifest_items,
    }
    manifest_path = archive_root / "manifest.json"
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_bytes(manifest_bytes)
    temporary.replace(manifest_path)
    validate_bundle(archive_root, manifest)
    admit_bundle(manifest, mode, load_policy(Path(__file__).parents[2] / "config/launch-modes.yaml"))
    return manifest


def validate_bundle(archive_root: Path, manifest: dict[str, Any]) -> None:
    """Validate manifest identity, uniqueness, safe paths, and file presence."""
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise BundleError("bundle manifest schema_version must be 1")
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise BundleError("bundle manifest requires run_id")
    if manifest.get("mode") not in {"production", "local", "eval"}:
        raise BundleError("bundle manifest requires a valid mode")
    if not isinstance(manifest.get("trusted"), bool):
        raise BundleError("bundle manifest requires a boolean trusted flag")
    items = manifest.get("items")
    if not isinstance(items, list):
        raise BundleError("bundle manifest items must be a list")

    archive_root = archive_root.resolve()
    manifest_path = archive_root / "manifest.json"
    seen_work_ids: set[str] = set()
    seen_paths: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise BundleError("bundle manifest items must be objects")
        work_id = item.get("work_id")
        if not isinstance(work_id, str) or not work_id or work_id in seen_work_ids:
            raise BundleError(f"invalid or duplicate bundle work_id: {work_id!r}")
        seen_work_ids.add(work_id)
        if not isinstance(item.get("issue_key"), str) or not item["issue_key"]:
            raise BundleError(f"bundle item {work_id} requires issue_key")
        artifact_map = item.get("artifacts")
        if not isinstance(artifact_map, dict) or not artifact_map:
            raise BundleError(f"bundle item {work_id} requires artifacts")
        for name, relative in artifact_map.items():
            if not isinstance(name, str) or not isinstance(relative, str) or not relative:
                raise BundleError(f"invalid artifact entry in bundle item {work_id}")
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts or relative in seen_paths:
                raise BundleError(f"unsafe or duplicate bundle artifact path: {relative}")
            destination = (archive_root / path).resolve()
            if not destination.is_relative_to(archive_root) or destination == manifest_path:
                raise BundleError(f"unsafe bundle artifact path: {relative}")
            if not destination.is_file() or not destination.read_bytes():
                raise BundleError(f"missing or empty bundle artifact: {relative}")
            seen_paths.add(relative)


def manifest_digest(manifest: dict[str, Any]) -> str:
    """Return the canonical digest used by publication receipts."""
    content = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    return hashlib.sha256(content).hexdigest()
