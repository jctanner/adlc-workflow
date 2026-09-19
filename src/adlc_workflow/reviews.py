"""Resolve bounded reviewer assignments from the active profile."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .artifacts import ArtifactLayout
from .selection import KEY_RE
from .workflow import WorkflowDefinitionError, stage


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
    assignments = []
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
        artifact = reviewer["result"]["artifact"]
        filename = artifact["filename"].format(issue_key=issue_key, reviewer_id=reviewer_id)
        if Path(filename).name != filename or filename in {"", ".", ".."}:
            raise WorkflowDefinitionError(f"unsafe reviewer filename: {filename}")
        destination = (layout.root(artifact["root"]) / filename).resolve()
        if not destination.is_relative_to(workspace_root) or destination in seen_paths:
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
            "output_path": str(destination),
        }
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
    return {
        "issue_key": issue_key,
        "execution": execution,
        "assignments": assignments,
        "aggregate_path": str(layout.review(issue_key)),
    }


def check_review_outputs(plan: dict[str, Any]) -> list[str]:
    """Return absent or empty outputs; caller also waits for agent success."""
    missing = []
    for assignment in plan["assignments"]:
        path = Path(assignment["output_path"])
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            missing.append(str(path))
    return missing
