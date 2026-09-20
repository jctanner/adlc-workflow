import pytest

from adlc_workflow.workflow import WorkflowDefinitionError, evaluate_gate, next_stage, stages


def profile() -> dict:
    return {
        "workflow": {
            "stages": [
                {"id": "refine", "outputs": ["strategy_markdown"], "gate": {
                    "source": {
                        "project": "RHAIRFE",
                        "issue_type": "Feature Request",
                        "labels": {"all": ["release-3.6"], "none": ["processing"]},
                        "fields": {"status": {"not_in": ["Closed"]}, "customfield_10855": {"exists": True}},
                    }
                }},
                {"id": "publish", "kind": "publication"},
            ]
        }
    }


def issue(**overrides: object) -> dict:
    fields = {
        "issuetype": {"name": "Feature Request"},
        "labels": ["release-3.6"],
        "status": "Approved",
        "customfield_10855": "3.6 GA RHOAI RELEASE",
    }
    fields.update(overrides)
    return {"key": "RHAIRFE-1", "fields": fields}


def test_profile_stage_order_is_explicit() -> None:
    loaded = profile()
    assert [item["id"] for item in stages(loaded)] == ["refine", "publish"]
    assert next_stage(loaded, "refine")["id"] == "publish"


def test_gate_accepts_matching_source_issue() -> None:
    assert evaluate_gate(stages(profile())[0], {"source": issue()}) == []


def test_gate_reports_label_and_field_failures() -> None:
    failures = evaluate_gate(
        stages(profile())[0],
        {"source": issue(labels=["processing"], status="Closed")},
    )
    assert any("labels.all" in failure for failure in failures)
    assert any("labels.none" in failure for failure in failures)
    assert any("fields.status" in failure for failure in failures)


def test_gate_requires_context() -> None:
    failures = evaluate_gate(stages(profile())[0], {})
    assert failures == ["gate.source: issue context is missing"]


def test_unknown_gate_subject_is_rejected() -> None:
    bad = {"id": "refine", "gate": {"jira": {}}}
    with pytest.raises(WorkflowDefinitionError, match="unsupported gate subject"):
        evaluate_gate(bad, {})


def test_linked_gate_supports_any_all_and_none() -> None:
    linked_stage = {
        "id": "refine",
        "gate": {
            "linked": {
                "any": [{"project": "RHAISTRAT", "labels": {"all": ["ready"]}}],
                "all": [{"fields": {"status": {"exists": True}}}],
                "none": [{"project": "RHAISTRAT", "labels": {"any": ["processing"]}}],
            }
        },
    }
    linked = [
        {"key": "RHAISTRAT-1", "fields": {"labels": ["ready"], "status": "Open"}},
        {"key": "RHAIRFE-2", "fields": {"labels": [], "status": "Open"}},
    ]
    assert evaluate_gate(linked_stage, {"linked": linked}) == []
    failures = evaluate_gate(
        linked_stage,
        {"linked": [{"key": "RHAISTRAT-1", "fields": {"labels": ["processing"], "status": "Open"}}]},
    )
    assert any("linked.none" in failure for failure in failures)
