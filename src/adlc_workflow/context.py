"""Profile-driven repository context preparation for a workflow run."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


class ContextError(RuntimeError):
    """Raised when configured context cannot be fetched or prepared."""


def selected_overlay_paths(workspace_root: str | Path) -> list[Path]:
    """Return the exact overlay files selected during context preparation."""
    workspace = Path(workspace_root).resolve()
    manifest_path = workspace / ".context" / "context-manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContextError(f"invalid prepared context manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ContextError("prepared context manifest must be an object")
    paths: list[Path] = []
    for source in manifest.get("sources", []):
        if not isinstance(source, dict) or not isinstance(source.get("destination"), str):
            continue
        destination = (workspace / source["destination"]).resolve()
        try:
            destination.relative_to(workspace)
        except ValueError as exc:
            raise ContextError(f"context destination escapes workspace: {destination}") from exc
        for overlay in source.get("applied_overlays", []):
            if not isinstance(overlay, dict) or not isinstance(overlay.get("path"), str):
                continue
            path = (destination / overlay["path"]).resolve()
            try:
                path.relative_to(workspace)
            except ValueError as exc:
                raise ContextError(f"context overlay escapes workspace: {path}") from exc
            if path.is_file():
                paths.append(path)
    return sorted(set(paths))


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise ContextError(f"git {' '.join(args)} failed: {detail.strip()}") from exc
    return result.stdout.strip()


def _matches(relative: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(relative, pattern) for pattern in patterns)


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    _, _, remainder = text.partition("\n")
    header, marker, _ = remainder.partition("\n---")
    if not marker:
        return {}
    value = yaml.safe_load(header) or {}
    return value if isinstance(value, dict) else {}


def _release_matches(release: Any, version: str) -> bool:
    if release in (None, "all"):
        return True
    values = release if isinstance(release, list) else [release]
    normalized = version.removeprefix("rhoai-")
    release_prefix = normalized.split("-", 1)[0]
    return any(
        value == "all"
        or (isinstance(value, str) and value in {version, normalized, release_prefix})
        for value in values
    )


def _overlay_matches(path: Path, version: str, components: list[str]) -> tuple[bool, dict[str, Any]]:
    metadata = _frontmatter(path)
    if metadata.get("status") != "active" or not _release_matches(metadata.get("release"), version):
        return False, metadata
    affects = metadata.get("affects", ["platform"])
    values = [str(value).casefold() for value in (affects if isinstance(affects, list) else [affects])]
    requested = {value.casefold() for value in components}
    if requested and "platform" not in values and not requested.intersection(values):
        return False, metadata
    return True, metadata


def _safe_destination(workspace_root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ContextError(f"context source requires {field}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ContextError(f"context source {field} must stay within workspace")
    return (workspace_root / path).resolve()


def prepare_context(
    install_root: Path,
    workspace_root: Path,
    profile: dict[str, Any],
    mode: str,
    components: list[str] | None = None,
) -> dict[str, Any]:
    """Fetch configured context and record the exact selected file contract."""
    sources = profile.get("context_sources", [])
    if not sources:
        return {"schema_version": 1, "status": "not_configured", "sources": []}
    context_base = workspace_root / ".context"
    context_base.mkdir(parents=True, exist_ok=True)
    requested_components = components or []
    install_root = Path(install_root).absolute()
    workspace_root = Path(workspace_root).resolve()
    manifests = []
    for source in sources:
        if not isinstance(source, dict):
            raise ContextError("each context source must be a mapping")
        source_id = source.get("id")
        repo_url = source.get("repo")
        ref = source.get("ref", "main")
        if not source_id or not repo_url:
            raise ContextError("each context source requires id and repo")
        destination = _safe_destination(workspace_root, source.get("destination", f".context/{source_id}"), "destination")
        include = source.get("include", ["**/*"])
        exclude = source.get("exclude", [])
        if not isinstance(include, list) or not all(isinstance(value, str) for value in include):
            raise ContextError(f"context source {source_id} include must be a list of strings")
        if not isinstance(exclude, list) or not all(isinstance(value, str) for value in exclude):
            raise ContextError(f"context source {source_id} exclude must be a list of strings")
        usage = source.get("usage")
        usage_path = None
        if usage is not None:
            usage_path = install_root / usage
            # `usage_path` is expected to be absolute after resolving it
            # against the absolute install root. Validate the configured
            # relative path before joining it instead.
            if Path(usage).is_absolute() or ".." in Path(usage).parts or not usage_path.is_file():
                raise ContextError(f"context source {source_id} usage document is invalid: {usage}")
            if not usage_path.read_text(encoding="utf-8").strip():
                raise ContextError(f"context source {source_id} usage document is empty: {usage}")
        version_file = source.get("version_file", "LATEST_VERSION")
        if not isinstance(version_file, str) or not version_file or Path(version_file).is_absolute() or ".." in Path(version_file).parts:
            raise ContextError(f"context source {source_id} version_file is invalid")
        with tempfile.TemporaryDirectory(prefix="adlc-context-") as temporary:
            checkout = Path(temporary) / "checkout"
            try:
                subprocess.run(
                    ["git", "clone", "--depth", "1", "--branch", str(ref), str(repo_url), str(checkout)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                detail = getattr(exc, "stderr", "") or str(exc)
                raise ContextError(f"context clone failed for {source_id}: {detail.strip()}") from exc
            commit = _git(checkout, "rev-parse", "HEAD")
            source_version_path = checkout / version_file
            if source_version_path.is_file():
                latest_version = source_version_path.read_text(encoding="utf-8").strip()
            else:
                versions = sorted((checkout / "architecture").glob("rhoai-*")) if (checkout / "architecture").is_dir() else []
                latest_version = versions[-1].name if versions else ""
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            destination.mkdir(parents=True, exist_ok=True)
            copied: list[str] = []
            applied_overlays: list[dict[str, Any]] = []
            for candidate in checkout.rglob("*"):
                if not candidate.is_file() or ".git" in candidate.parts:
                    continue
                relative = candidate.relative_to(checkout).as_posix()
                if not _matches(relative, include) or _matches(relative, exclude):
                    continue
                if relative.startswith("overlays/") and candidate.suffix == ".md":
                    matches, metadata = _overlay_matches(candidate, latest_version, requested_components)
                    if not matches:
                        continue
                    applied_overlays.append({
                        "path": relative,
                        "id": metadata.get("id", candidate.stem),
                        "release": metadata.get("release", "all"),
                        "affects": metadata.get("affects", ["platform"]),
                    })
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, target)
                copied.append(relative)
            version_destination = destination / version_file
            version_destination.parent.mkdir(parents=True, exist_ok=True)
            version_destination.write_text(latest_version + "\n", encoding="utf-8")
            if version_file not in copied:
                copied.append(version_file)
            manifests.append({
                "id": source_id,
                "repo": repo_url,
                "ref": ref,
                "commit": commit,
                "destination": str(destination.relative_to(workspace_root)),
                "version_file": version_file,
                "latest_version": latest_version,
                "usage": str(usage_path.relative_to(install_root)) if usage_path else None,
                "applied_overlays": sorted(applied_overlays, key=lambda item: item["path"]),
                "file_count": len(copied),
                "files_sha256": hashlib.sha256("\n".join(sorted(copied)).encode("utf-8")).hexdigest(),
            })
    manifest = {
        "schema_version": 1,
        "status": "prepared",
        "prepared_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "components": sorted(requested_components),
        "sources": manifests,
    }
    manifest_path = context_base / "context-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
