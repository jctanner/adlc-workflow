"""Resolve bounded reviewer assignments from the active profile."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .artifacts import ArtifactLayout, ArtifactLayoutError
from .profiles import resource_path
from .selection import KEY_RE
from .workflow import WorkflowDefinitionError, stage


def _context_overlay_paths(workspace_root: Path) -> list[str]:
    """Return exact overlay files selected by the context adapter."""
    manifest_path = workspace_root / ".context/context-manifest.json"
    if not manifest_path.is_file():
        return []
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    paths: list[str] = []
    for source in manifest.get("sources", []):
        if not isinstance(source, dict) or not isinstance(source.get("destination"), str):
            continue
        for overlay in source.get("applied_overlays", []):
            if isinstance(overlay, dict) and isinstance(overlay.get("path"), str):
                paths.append(str(workspace_root / source["destination"] / overlay["path"]))
    return sorted(paths)


def review_plan(install_root: Path, workspace_root: Path, profile: dict[str, Any],
                issue_key: str, profile_path: str) -> dict[str, Any]:
    if not KEY_RE.fullmatch(issue_key):
        raise WorkflowDefinitionError(f"invalid issue key: {issue_key!r}")
    install_root = install_root.absolute()
    workspace_root = workspace_root.resolve()
    review = stage(profile, "review")
    execution = review.get("reviewer_execution", "sequential")
    if execution not in {"parallel", "sequential"}:
        raise WorkflowDefinitionError(f"unsupported reviewer execution: {execution}")
    layout = ArtifactLayout(workspace_root, profile)
    plugin_name = json.loads((install_root / ".claude-plugin/plugin.json").read_text())["name"]
    reviewers = review.get("reviewers", [])
    if not reviewers:
        raise WorkflowDefinitionError("review requires reviewers")
    aggregate = review.get("aggregate")
    if not isinstance(aggregate, dict):
        raise WorkflowDefinitionError("review requires aggregate configuration")
    aggregate_inputs = aggregate.get("inputs")
    if not isinstance(aggregate_inputs, list) or not aggregate_inputs or not all(
        isinstance(value, str) and value for value in aggregate_inputs
    ):
        raise WorkflowDefinitionError("review.aggregate.inputs must be a non-empty list")
    aggregate_schema = aggregate.get("schema")
    if not isinstance(aggregate_schema, str) or not aggregate_schema:
        raise WorkflowDefinitionError("review.aggregate.schema must be a non-empty path")
    renderer = aggregate.get("renderer")
    if not isinstance(renderer, str) or not renderer:
        raise WorkflowDefinitionError("review.aggregate.renderer must be a non-empty identifier")
    assignments = []
    aggregate_schema_path = resource_path(
        install_root, profile, aggregate_schema, "workflow.stages.review.aggregate.schema"
    )
    reviewer_paths = layout.reviewer_paths(issue_key)
    seen_ids: set[str] = set()
    seen_paths: set[Path] = {layout.review(issue_key).resolve()}
    for reviewer in reviewers:
        reviewer_id = reviewer["id"]
        if reviewer_id in seen_ids:
            raise WorkflowDefinitionError(f"duplicate reviewer: {reviewer_id}")
        seen_ids.add(reviewer_id)
        worker = reviewer["worker"]
        if not re.fullmatch(r"agent:[a-z0-9-]+", worker):
            raise WorkflowDefinitionError(f"reviewer must reference an agent: {worker}")
        agent_name = worker.removeprefix("agent:")
        if not (install_root / "agents" / f"{agent_name}.md").is_file():
            raise WorkflowDefinitionError(f"missing reviewer agent: {agent_name}")
        destination = reviewer_paths[reviewer_id]
        if destination in seen_paths:
            raise WorkflowDefinitionError(f"unsafe or shared reviewer output: {destination}")
        seen_paths.add(destination)
        inputs = {
            "issue_key": issue_key,
            "reviewer_id": reviewer_id,
            "strategy_path": str(layout.task(issue_key)),
            "profile_path": str(install_root / profile_path),
            "rubric_path": str(install_root / review["scoring"]["rubric"]["path"]),
            "context_manifest": str(workspace_root / ".context/context-manifest.json"),
            "context_sources": [{
                "destination": str(workspace_root / source["destination"]),
                "usage": str(install_root / source["usage"]),
            } for source in profile.get("context_sources", [])],
            "context_overlays": _context_overlay_paths(workspace_root),
            "output_path": str(destination),
        }
        try:
            inputs["decomposition_path"] = str(layout.generated("decomposition", issue_key))
        except ArtifactLayoutError:
            pass
        try:
            inputs["epic_tasks_root"] = str(layout.root("epics"))
        except ArtifactLayoutError:
            pass
        assignments.append({
            "reviewer_id": reviewer_id,
            "worker": worker,
            "output_path": str(destination),
            "task": {
                "subagent_type": f"{plugin_name}:{agent_name}",
                "description": f"{reviewer_id} review for {issue_key}",
                "run_in_background": execution == "parallel",
                "prompt": "Perform only your assigned review using these paths.\n" + json.dumps(inputs, indent=2),
            },
        })
    reviewer_ids = {assignment["reviewer_id"] for assignment in assignments}
    unknown_inputs = set(aggregate_inputs) - reviewer_ids
    if unknown_inputs:
        raise WorkflowDefinitionError(
            f"review.aggregate.inputs reference unknown reviewers: {sorted(unknown_inputs)}"
        )
    return {
        "issue_key": issue_key,
        "execution": execution,
        "assignments": assignments,
        "aggregate_path": str(layout.review(issue_key)),
        "aggregate": {
            "inputs": aggregate_inputs,
            "schema": str(aggregate_schema_path),
            "renderer": renderer,
        },
    }


def check_review_outputs(plan: dict[str, Any]) -> list[str]:
    """Return absent or empty outputs without waiting."""
    missing = []
    for assignment in plan["assignments"]:
        path = Path(assignment["output_path"])
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            missing.append(str(path))
    return missing


def wait_for_review_outputs(
    plan: dict[str, Any],
    timeout_seconds: float = 30.0,
    poll_seconds: float = 0.25,
) -> list[str]:
    """Wait for every reviewer output to become a non-empty file.

    Native agent completion and the filesystem write are separate events. The
    bounded wait makes that handoff explicit while still failing promptly when
    a reviewer really did not produce its assigned output.
    """
    if timeout_seconds < 0:
        raise ValueError("timeout_seconds must be non-negative")
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be positive")
    deadline = time.monotonic() + timeout_seconds
    missing = check_review_outputs(plan)
    while missing and time.monotonic() < deadline:
        time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
        missing = check_review_outputs(plan)
    return missing
