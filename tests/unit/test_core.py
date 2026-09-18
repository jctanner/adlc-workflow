import json
import shutil
from pathlib import Path

import pytest
import yaml

from adlc_workflow.core import WorkflowCore, WorkflowError
from adlc_workflow.selection import SelectionError, normalize_request


def request(*keys: str) -> dict:
    return {
        "args": list(keys),
        "kwargs": {
            "mode": "eval",
            "identity": "test",
            "source_issue": {
                "key": keys[0] if keys else "RHAIRFE-1",
                "fields": {
                    "issuetype": {"name": "Feature Request"},
                    "labels": ["rfe-creator-autofix-rubric-pass", "strat-creator-3.6"],
                    "status": "Approved",
                    "customfield_10855": "3.6 GA RHOAI RELEASE",
                },
            },
        },
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    source_root = Path(__file__).parents[2]
    shutil.copytree(source_root / "config", tmp_path / "config")
    profile_path = tmp_path / "config" / "rhai-feature-creator.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["context_sources"] = []
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))
    return tmp_path


def test_request_normalization_deduplicates_keys() -> None:
    normalized = normalize_request(request("RHAIRFE-1", "RHAIRFE-1"), default_profile="profile.yaml")
    assert normalized.args == ("RHAIRFE-1",)
    assert normalized.selector == "keys"


def test_request_rejects_conflicting_selector() -> None:
    with pytest.raises(SelectionError, match="cannot be combined"):
        normalize_request(
            {"args": ["RHAIRFE-1"], "kwargs": {"mode": "eval", "jql_default": True}},
            default_profile="profile.yaml",
        )


def test_core_emits_refine_and_review_tasks(project: Path) -> None:
    core = WorkflowCore(project)
    started = core.start(request("RHAIRFE-1"))
    run_id = started["run_id"]

    refine = core.advance(run_id)
    assert refine["kind"] == "task"
    assert refine["stage"] == "refine"
    refine_result = {
        "task_id": refine["task_id"],
        "run_id": run_id,
        "revision": refine["expected_revision"],
        "outputs": {"strategy_markdown": "## Strategy\n\nA bounded approach."},
    }
    core.submit(run_id, refine["task_id"], refine_result)

    review = core.advance(run_id)
    assert review["stage"] == "review"
    review_result = {
        "task_id": review["task_id"],
        "run_id": run_id,
        "revision": review["expected_revision"],
        "outputs": {"review_markdown": "# Review\n\nNeeds validation."},
    }
    core.submit(run_id, review["task_id"], review_result)
    (project / "artifacts" / "rhai-feature-tasks").mkdir(parents=True)
    (project / "artifacts" / "rhai-feature-tasks" / "RHAIRFE-1.md").write_text("## Strategy\n")
    (project / "artifacts" / "rhai-feature-reviews").mkdir(parents=True)
    (project / "artifacts" / "rhai-feature-reviews" / "RHAIRFE-1-review.md").write_text("# Review\n")
    complete = core.advance(run_id)
    assert complete["kind"] == "complete"
    assert complete["publication"]["status"] == "complete"
    assert (project / "artifacts" / "rhai-feature-published" / run_id / "manifest.json").is_file()


def test_core_preserves_per_issue_gate_context_for_batch(project: Path) -> None:
    document = request("RHAIRFE-10", "RHAIRFE-11")
    document["kwargs"]["source_issues"] = {
        key: {"key": key, "fields": {
            "issuetype": {"name": "Feature Request"},
            "labels": ["rfe-creator-rubric-pass"],
            "status": "New",
            "customfield_10855": "3.6 GA RHOAI RELEASE",
        }}
        for key in ("RHAIRFE-10", "RHAIRFE-11")
    }
    document["kwargs"].pop("source_issue")
    core = WorkflowCore(project)
    started = core.start(document)
    state = core.status(started["run_id"])
    assert set(state["gate_context"]["sources"]) == {"RHAIRFE-10", "RHAIRFE-11"}
    first = core.advance(started["run_id"])
    assert first["issue_key"] == "RHAIRFE-10"


def test_core_uses_profile_selected_artifact_roots(project: Path) -> None:
    profile_path = project / "config" / "rhai-feature-creator.yaml"
    profile = yaml.safe_load(profile_path.read_text())
    profile["artifacts"]["bundle_root"] = "bundle"
    profile["artifacts"]["roots"] = {
        "tasks": "bundle/feature-docs",
        "originals": "bundle/source",
        "reviews": "bundle/assessments",
        "inputs": "bundle/context",
        "extensions": "bundle/extensions",
        "reports": "bundle/reports",
        "published": "bundle/published",
    }
    profile["artifacts"]["evidence_files"] = {
        "selection": "selection.json",
        "result": "result.json",
        "pipeline_data": "pipeline-data.json",
    }
    profile_path.write_text(yaml.safe_dump(profile, sort_keys=False))

    core = WorkflowCore(project)
    started = core.start(request("RHAIRFE-5"))

    assert (project / "bundle" / "selection.json").is_file()
    assert not (project / "artifacts" / "selection.json").exists()
    assert (project / "bundle" / "context" / "context-manifest.json").is_file()
    assert started["selection_path"] == "bundle/selection.json"


def test_core_rejects_stale_result(project: Path) -> None:
    core = WorkflowCore(project)
    run_id = core.start(request("RHAIRFE-2"))["run_id"]
    task = core.advance(run_id)
    bad_result = {
        "task_id": task["task_id"],
        "run_id": run_id,
        "revision": 99,
        "outputs": {"strategy_markdown": "## Strategy"},
    }
    with pytest.raises(WorkflowError, match="revision"):
        core.submit(run_id, task["task_id"], bad_result)


def test_empty_selection_is_explicit_noop(project: Path) -> None:
    core = WorkflowCore(project)
    started = core.start({"args": [], "kwargs": {"mode": "eval"}})
    assert started["status"] == "complete"
    assert started["items"] == []
    selection = json.loads((project / "artifacts" / "rhai-feature-selection.json").read_text())
    assert selection["selection_status"] == "empty"


def test_install_and_workspace_roots_are_separate(project: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "runtime"
    core = WorkflowCore(project, workspace)
    core.start(request("RHAIRFE-3"))
    assert (workspace / "artifacts" / "rhai-feature-selection.json").is_file()
    assert not (project / "artifacts" / "rhai-feature-selection.json").exists()


def test_linked_install_root_accepts_symlinked_profile(project: Path, tmp_path: Path) -> None:
    linked = tmp_path / "cache" / "adlc-workflow"
    linked.parent.mkdir()
    linked.symlink_to(project, target_is_directory=True)

    core = WorkflowCore(linked, tmp_path / "runtime")
    started = core.start(request("RHAIRFE-4"))

    assert started["status"] == "running"
