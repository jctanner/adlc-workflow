"""Coordinate independent publication effects and emit verified receipts."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .adapters.eval import EvalPublisher
from .adapters.git import LocalArchivePublisher
from .adapters.jira import JiraPublisher
from .artifacts import ArtifactLayout
from .workflow import WorkflowDefinitionError, publication_stage


class PublicationError(RuntimeError):
    """Raised when a required publication effect fails."""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def publish_run(install_root: Path, workspace_root: Path, profile: dict[str, Any], state: dict[str, Any], run_id: str) -> dict[str, Any]:
    mode = state.get("mode", "local")
    items = state.get("items", [])
    artifacts = ArtifactLayout(workspace_root, profile)
    publish_stage = publication_stage(profile)
    configured = publish_stage.get("adapters", {}).get(mode, [])
    if not configured:
        raise PublicationError(f"publication adapters are not configured for mode {mode}")
    receipts: list[dict[str, Any]] = []
    try:
        mapping = {}
        mapping_ref = publish_stage.get("jira_mapping")
        if mapping_ref:
            mapping_path = install_root / mapping_ref
            mapping = yaml.safe_load(mapping_path.read_text(encoding="utf-8")) or {}
        if "jira" in configured:
            for item in items:
                strategy = artifacts.task(item["key"])
                review = artifacts.review(item["key"])
                receipts.append(JiraPublisher(mapping=mapping).publish(item["key"], run_id, item["work_id"], str(strategy), str(review)))
        if mode == "eval" and "archive" in configured:
            receipts.append(EvalPublisher().publish(workspace_root, run_id, items, profile))
        elif "archive" in configured:
            receipts.append(LocalArchivePublisher().publish(workspace_root, run_id, items, profile))
        unknown = set(configured) - {"jira", "archive"}
        if unknown:
            raise PublicationError(f"unknown publication adapters: {sorted(unknown)}")
    except (PublicationError, RuntimeError, WorkflowDefinitionError) as exc:
        raise PublicationError(str(exc)) from exc

    result = {
        "schema_version": 1,
        "run_id": run_id,
        "status": "complete",
        "mode": mode,
        "published_at": _now(),
        "receipts": receipts,
    }
    result_path = artifacts.evidence_path("result")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result
