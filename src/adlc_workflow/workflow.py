"""Profile-defined workflow stages and deterministic stage gates."""

from __future__ import annotations

from typing import Any


class WorkflowDefinitionError(ValueError):
    """Raised when a profile workflow or gate is malformed."""


def stages(profile: dict[str, Any]) -> list[dict[str, Any]]:
    workflow = profile.get("workflow")
    if not isinstance(workflow, dict):
        raise WorkflowDefinitionError("profile requires a workflow mapping")
    value = workflow.get("stages")
    if not isinstance(value, list) or not value:
        raise WorkflowDefinitionError("profile workflow requires non-empty stages")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for stage in value:
        if not isinstance(stage, dict) or not isinstance(stage.get("id"), str) or not stage["id"]:
            raise WorkflowDefinitionError("each workflow stage requires a non-empty id")
        if stage["id"] in seen:
            raise WorkflowDefinitionError(f"duplicate workflow stage: {stage['id']}")
        seen.add(stage["id"])
        result.append(stage)
    return result


def stage(profile: dict[str, Any], stage_id: str) -> dict[str, Any]:
    for candidate in stages(profile):
        if candidate["id"] == stage_id:
            return candidate
    raise WorkflowDefinitionError(f"profile has no workflow stage: {stage_id}")


def next_stage(profile: dict[str, Any], current: str | None) -> dict[str, Any] | None:
    ordered = stages(profile)
    if current is None:
        return ordered[0]
    for index, candidate in enumerate(ordered):
        if candidate["id"] == current:
            return ordered[index + 1] if index + 1 < len(ordered) else None
    raise WorkflowDefinitionError(f"unknown workflow stage: {current}")


def publication_stage(profile: dict[str, Any]) -> dict[str, Any]:
    matches = [candidate for candidate in stages(profile) if candidate.get("kind") == "publication"]
    if len(matches) != 1:
        raise WorkflowDefinitionError("profile workflow requires exactly one publication stage")
    return matches[0]


def stage_outputs(stage_definition: dict[str, Any]) -> list[str]:
    outputs = stage_definition.get("outputs", [])
    if isinstance(outputs, str):
        outputs = [outputs]
    if not isinstance(outputs, list):
        raise WorkflowDefinitionError(f"stage {stage_definition['id']} outputs must be a list")
    names = []
    for item in outputs:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str) or not name:
            raise WorkflowDefinitionError(f"stage {stage_definition['id']} outputs must contain names")
        names.append(name)
    return names


def stage_output_artifacts(stage_definition: dict[str, Any]) -> dict[str, str]:
    """Return output names mapped to ArtifactLayout targets."""
    outputs = stage_definition.get("outputs", [])
    if isinstance(outputs, str):
        return {}
    return {
        item["name"]: item["artifact"]
        for item in outputs
        if isinstance(item, dict) and item.get("artifact")
    }


def evaluate_gate(stage_definition: dict[str, Any], context: dict[str, Any]) -> list[str]:
    """Return deterministic explanations for a stage gate failure.

    A gate is expressed as ``source`` or ``parent`` issue predicates, plus
    ``linked.any``, ``linked.all``, and ``linked.none`` collections. Each issue
    predicate supports project, issue_type, labels (all/any/none), and fields
    (equals/in/not_in/exists). Missing context is a failure, never an implicit
    pass.
    """
    gate = stage_definition.get("gate")
    if gate is None:
        return []
    if not isinstance(gate, dict):
        raise WorkflowDefinitionError(f"stage {stage_definition['id']} gate must be a mapping")
    failures: list[str] = []
    for subject, requirements in gate.items():
        if subject == "linked":
            failures.extend(_evaluate_linked(requirements, context.get("linked")))
            continue
        if subject not in {"source", "parent"}:
            raise WorkflowDefinitionError(f"unsupported gate subject: {subject}")
        if not isinstance(requirements, dict):
            raise WorkflowDefinitionError(f"stage {stage_definition['id']} gate.{subject} must be a mapping")
        issue = context.get(subject)
        if not isinstance(issue, dict):
            failures.append(f"gate.{subject}: issue context is missing")
            continue
        failures.extend(_evaluate_issue(subject, issue, requirements))
    return failures


def _evaluate_linked(requirements: Any, linked: Any) -> list[str]:
    if not isinstance(requirements, dict):
        raise WorkflowDefinitionError("gate.linked must be a mapping")
    if not isinstance(linked, list):
        linked = []
    failures: list[str] = []
    for operator in ("any", "all", "none"):
        rules = requirements.get(operator, [])
        if not isinstance(rules, list):
            raise WorkflowDefinitionError(f"gate.linked.{operator} must be a list")
        matches = [
            any(not _evaluate_issue("linked", issue, rule) for issue in linked if isinstance(issue, dict))
            for rule in rules
            if isinstance(rule, dict)
        ]
        if len(matches) != len(rules):
            raise WorkflowDefinitionError(f"gate.linked.{operator} entries must be mappings")
        if operator == "any" and rules and not any(matches):
            failures.append("gate.linked.any: no linked issue matched")
        if operator == "all" and any(not match for match in matches):
            failures.append("gate.linked.all: a linked issue requirement did not match")
        if operator == "none" and any(matches):
            failures.append("gate.linked.none: a forbidden linked issue matched")
    return failures


def _evaluate_issue(subject: str, issue: dict[str, Any], requirements: dict[str, Any]) -> list[str]:
    fields = issue.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    failures: list[str] = []
    project = issue.get("key", "").split("-", 1)[0]
    expected_project = requirements.get("project")
    if expected_project is not None and project not in _values(expected_project):
        failures.append(f"gate.{subject}.project: expected {expected_project!r}, got {project!r}")
    issue_type = (fields.get("issuetype") or {}).get("name") if isinstance(fields.get("issuetype"), dict) else fields.get("issuetype")
    expected_type = requirements.get("issue_type")
    if expected_type is not None and issue_type not in _values(expected_type):
        failures.append(f"gate.{subject}.issue_type: expected {expected_type!r}, got {issue_type!r}")
    labels = fields.get("labels", [])
    if not isinstance(labels, list):
        labels = [labels]
    label_rules = requirements.get("labels", {})
    if not isinstance(label_rules, dict):
        raise WorkflowDefinitionError(f"gate.{subject}.labels must be a mapping")
    for label in _values(label_rules.get("all", [])):
        if label not in labels:
            failures.append(f"gate.{subject}.labels.all: missing {label!r}")
    any_labels = _values(label_rules.get("any", []))
    if any_labels and not any(label in labels for label in any_labels):
        failures.append(f"gate.{subject}.labels.any: none of {any_labels!r} present")
    for label in _values(label_rules.get("none", [])):
        if label in labels:
            failures.append(f"gate.{subject}.labels.none: forbidden {label!r} present")
    field_rules = requirements.get("fields", {})
    if not isinstance(field_rules, dict):
        raise WorkflowDefinitionError(f"gate.{subject}.fields must be a mapping")
    for name, rule in field_rules.items():
        if not isinstance(rule, dict):
            rule = {"equals": rule}
        value = fields.get(name)
        if "exists" in rule and bool(rule["exists"]) != (name in fields and value not in (None, "", [])):
            failures.append(f"gate.{subject}.fields.{name}: exists={rule['exists']} failed")
        if "equals" in rule and value != rule["equals"]:
            failures.append(f"gate.{subject}.fields.{name}: expected {rule['equals']!r}, got {value!r}")
        if "in" in rule and value not in _values(rule["in"]):
            failures.append(f"gate.{subject}.fields.{name}: {value!r} not in {rule['in']!r}")
        if "not_in" in rule and value in _values(rule["not_in"]):
            failures.append(f"gate.{subject}.fields.{name}: {value!r} is excluded")
    return failures


def _values(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]
