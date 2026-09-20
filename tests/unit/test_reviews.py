from pathlib import Path

import pytest
import yaml
from threading import Thread
from time import sleep

from adlc_workflow.artifacts import ArtifactLayout, ArtifactLayoutError
from adlc_workflow.profiles import load_profile
from adlc_workflow.reviews import check_review_outputs, review_plan, wait_for_review_outputs
from adlc_workflow.review_trace import verify_parallel_reviewers
from adlc_workflow.workflow import WorkflowDefinitionError

INSTALL = Path(__file__).parents[2]
PROFILE = "config/rhai-feature-creator.yaml"


def test_agent_assignments_and_output_barrier(tmp_path):
    profile = load_profile(INSTALL, PROFILE)
    profile["artifacts"]["roots"]["reviews"] = "custom/reviews"
    plan = review_plan(INSTALL, tmp_path, profile, "RHAIRFE-7", PROFILE)
    assert len(plan["assignments"]) == 5
    assert len(check_review_outputs(plan)) == 5
    paths = set()
    for assignment in plan["assignments"]:
        task = assignment["task"]
        assert task["run_in_background"] is True
        name = task["subagent_type"].split(":", 1)[1]
        definition = (INSTALL / "agents" / f"{name}.md").read_text()
        metadata = yaml.safe_load(definition.split("---", 2)[1])
        assert metadata["name"] == name
        assert "Skill" not in metadata["tools"]
        assert "Task" not in metadata["tools"]
        assert not (INSTALL / "skills" / name / "SKILL.md").exists()
        path = Path(assignment["output_path"])
        assert path.parent == tmp_path / "custom/reviews"
        paths.add(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(" ")
    assert len(paths) == 5
    assert len(check_review_outputs(plan)) == 5
    for path in paths:
        path.write_text("# RHAIRFE-7 review\nEvidence and findings.\n")
    assert check_review_outputs(plan) == []


def test_output_barrier_waits_for_late_reviewer_write(tmp_path):
    profile = load_profile(INSTALL, PROFILE)
    plan = review_plan(INSTALL, tmp_path, profile, "RHAIRFE-7", PROFILE)
    path = Path(plan["assignments"][0]["output_path"])
    path.parent.mkdir(parents=True, exist_ok=True)

    def write_late_output():
        sleep(0.05)
        path.write_text("# delayed review\n")

    writer = Thread(target=write_late_output)
    writer.start()
    missing = wait_for_review_outputs(plan, timeout_seconds=1.0, poll_seconds=0.01)
    writer.join()

    assert str(path) not in missing
    assert len(missing) == 4


def test_output_barrier_rejects_invalid_timing_values(tmp_path):
    profile = load_profile(INSTALL, PROFILE)
    plan = review_plan(INSTALL, tmp_path, profile, "RHAIRFE-7", PROFILE)
    with pytest.raises(ValueError, match="non-negative"):
        wait_for_review_outputs(plan, timeout_seconds=-1)
    with pytest.raises(ValueError, match="positive"):
        wait_for_review_outputs(plan, poll_seconds=0)


@pytest.mark.parametrize("filename", ["../escaped.md", "{issue_key}-review.md", "shared.md"])
def test_rejects_escaping_or_colliding_outputs(tmp_path, filename):
    profile = load_profile(INSTALL, PROFILE)
    reviewers = profile["workflow"]["stages"][1]["reviewers"]
    for reviewer in reviewers:
        reviewer["result"]["artifact"]["filename"] = filename
    with pytest.raises(WorkflowDefinitionError, match="unsafe|shared"):
        review_plan(INSTALL, tmp_path, profile, "RHAIRFE-7", PROFILE)


def test_rejects_skill_workers(tmp_path):
    profile = load_profile(INSTALL, PROFILE)
    profile["workflow"]["stages"][1]["reviewers"][0]["worker"] = "skill:old-reviewer"
    with pytest.raises(WorkflowDefinitionError, match="must reference an agent"):
        review_plan(INSTALL, tmp_path, profile, "RHAIRFE-7", PROFILE)


@pytest.mark.parametrize("target", ["aggregate", "reviewer"])
def test_rejects_reserved_review_artifact_names(tmp_path, target):
    profile = load_profile(INSTALL, PROFILE)
    review = profile["workflow"]["stages"][1]
    if target == "aggregate":
        review["aggregate"]["artifact"]["filename"] = "manifest.json"
    else:
        review["reviewers"][0]["result"]["artifact"]["filename"] = "rhai-feature-result.json"
    with pytest.raises(ArtifactLayoutError, match="reserved"):
        ArtifactLayout(tmp_path, profile)


def start(name):
    return {"type": "system", "subtype": "task_started", "task_id": name, "subagent_type": name}


def end(name, status="completed"):
    return {"type": "system", "subtype": "task_notification", "task_id": name, "status": status}


def test_trace_checks_execution_overlap():
    assert verify_parallel_reviewers([start("a"), start("b"), end("a"), end("b")], {"a", "b"})["count"] == 2
    with pytest.raises(ValueError, match="before the first completion"):
        verify_parallel_reviewers([start("a"), end("a"), start("b"), end("b")], {"a", "b"})


def test_trace_rejects_missing_or_failed_workers():
    with pytest.raises(ValueError, match="missing"):
        verify_parallel_reviewers([start("a"), end("a")], {"a", "b"})
    with pytest.raises(ValueError, match="did not succeed"):
        verify_parallel_reviewers([start("a"), end("a", "failed")], {"a"})
