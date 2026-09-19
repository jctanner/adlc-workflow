#!/usr/bin/env python3
"""Live native-agent smoke test. Uses fixture documents; never touches Jira."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

INSTALL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(INSTALL / "src"))

from adlc_workflow.profiles import load_profile
from adlc_workflow.reviews import check_review_outputs, review_plan
from adlc_workflow.review_trace import verify_parallel_reviewers


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="adlc-review-smoke-"))
    profile_path = "config/rhai-feature-creator.yaml"
    profile = load_profile(INSTALL, profile_path)
    rubric = workspace / "rubric.yaml"
    rubric.write_text("dimensions: [feasibility, testability, scope, architecture]\n"
                      "scale: '0 = missing, 1 = partial, 2 = supported'\n"
                      "total: sum of four scores, maximum 8\npass_threshold: 6\n")
    for stage in profile["workflow"]["stages"]:
        if stage["id"] == "review":
            stage["scoring"]["rubric"]["path"] = str(rubric)
    context = workspace / ".context/architecture-context"
    (context / "architecture/fixture").mkdir(parents=True)
    (context / "overlays").mkdir()
    (context / "LATEST_VERSION").write_text("fixture\n")
    (context / "architecture/fixture/PLATFORM.md").write_text(
        "# Fixture platform\nThe dashboard serves a static, read-only catalog. "
        "A Git repository supplies metadata. There are no operators or external APIs.\n")
    (workspace / ".context/context-manifest.json").write_text(json.dumps({
        "status": "prepared", "sources": [{"destination": str(context), "commit": "fixture"}],
    }))
    strategy = workspace / "artifacts/rhai-feature-tasks/RHAIRFE-991.md"
    strategy.parent.mkdir(parents=True)
    strategy.write_text(
        "# RHAIRFE-991 fixture\n## Business Need\nUsers need a read-only MCP server index.\n"
        "## Strategy\nThe dashboard renders Git-maintained server metadata. The dashboard "
        "team owns it; one sprint. No runtime installation or credentials are in scope.\n"
        "Acceptance: known entries display their names and documentation links; empty "
        "catalogs show a message; invalid entries are rejected by CI.\n")
    plan = review_plan(INSTALL, workspace, profile, "RHAIRFE-991", profile_path)
    for assignment in plan["assignments"]:
        Path(assignment["output_path"]).parent.mkdir(parents=True, exist_ok=True)
        assignment["task"]["prompt"] += (
            "\nThis is a tiny concurrency fixture. Keep your review under 150 words. "
            "Use the supplied fixture rubric, not the production profile's rubric.")
    prompt = (
        "Run this native reviewer concurrency smoke test. Launch every assignment with "
        "the Task tool (Agent in some versions), using its exact task object including "
        "run_in_background=true. Start ALL five before waiting for ANY. Then wait for "
        "every completion and report the five output paths. Do not call skills, spawn "
        "wrapper agents, write reviews yourself, use shell commands, or access Jira.\n"
        + json.dumps(plan)
    )
    log = workspace / "trace.jsonl"
    print(f"Smoke workspace: {workspace}\nLog: {log}", flush=True)
    started = time.monotonic()
    with log.open("w") as output:
        result = subprocess.run([
            "claude", "--model", os.environ.get("ADLC_CLAUDE_MODEL", "claude-haiku-4-5"),
            "--dangerously-skip-permissions", "--output-format=stream-json", "--verbose",
            "--max-budget-usd", "1", "--no-session-persistence", "-p", prompt,
        ], cwd=workspace, stdout=output, stderr=subprocess.STDOUT, timeout=240, check=False)
    events = []
    for line in log.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            pass
    if result.returncode or not any(e.get("type") == "result" and not e.get("is_error") for e in events):
        raise RuntimeError(f"Claude smoke failed; inspect {log}")
    report = verify_parallel_reviewers(events, {a["task"]["subagent_type"] for a in plan["assignments"]})
    missing = check_review_outputs(plan)
    if missing:
        raise RuntimeError(f"missing reviewer files: {missing}")
    report.update(elapsed_seconds=round(time.monotonic() - started, 1), log=str(log))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
