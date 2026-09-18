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


class ContextError(RuntimeError):
    """Raised when configured context cannot be fetched or prepared."""


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


def prepare_context(install_root: Path, workspace_root: Path, profile: dict[str, Any], mode: str) -> dict[str, Any]:
    """Fetch configured context into the runtime workspace and return its manifest.

    Every execution mode uses the configured source. Evaluation profiles that
    must be hermetic should provide a fixture repository or omit external
    context explicitly; they must not silently run refinement without context.
    """
    sources = profile.get("context_sources", [])
    if not sources:
        return {"schema_version": 1, "status": "not_configured", "sources": []}
    context_base = workspace_root / ".context"
    context_base.mkdir(parents=True, exist_ok=True)
    manifests = []
    for source in sources:
        source_id = source.get("id")
        repo_url = source.get("repo")
        ref = source.get("ref", "main")
        if not source_id or not repo_url:
            raise ContextError("each context source requires id and repo")
        destination = workspace_root / source.get("destination", f".context/{source_id}")
        include = source.get("include", ["**/*"])
        exclude = source.get("exclude", [])
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
            if destination.exists() or destination.is_symlink():
                if destination.is_dir() and not destination.is_symlink():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            destination.mkdir(parents=True, exist_ok=True)
            copied = []
            for candidate in checkout.rglob("*"):
                if not candidate.is_file() or ".git" in candidate.parts:
                    continue
                relative = candidate.relative_to(checkout).as_posix()
                if not _matches(relative, include) or _matches(relative, exclude):
                    continue
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(candidate, target)
                copied.append(relative)
            versions = sorted((destination / "architecture").glob("rhoai-*")) if (destination / "architecture").is_dir() else []
            if versions:
                (destination / "LATEST_VERSION").write_text(versions[-1].name + "\n", encoding="utf-8")
            manifests.append({
                "id": source_id,
                "repo": repo_url,
                "ref": ref,
                "commit": commit,
                "destination": str(destination.relative_to(workspace_root)),
                # Keep the runtime manifest small enough for an agent to read.
                # The checkout itself is the authoritative file tree; callers
                # can use this digest to detect which selected file set was
                # prepared without embedding hundreds of paths in the prompt.
                "file_count": len(copied),
                "files_sha256": hashlib.sha256(
                    "\n".join(sorted(copied)).encode("utf-8")
                ).hexdigest(),
            })
    manifest = {
        "schema_version": 1,
        "status": "prepared",
        "prepared_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "sources": manifests,
    }
    manifest_path = context_base / "context-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
