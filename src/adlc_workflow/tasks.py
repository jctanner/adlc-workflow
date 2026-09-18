"""Task envelopes and bounded result acceptance."""

from __future__ import annotations

from typing import Any


class TaskError(ValueError):
    """Raised when a worker result does not match its task."""


def validate_result(task: dict[str, Any], result: dict[str, Any]) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("outputs"), dict):
        raise TaskError("result must contain an outputs object")
    if result.get("task_id") != task["task_id"]:
        raise TaskError("result task_id does not match the task")
    if result.get("run_id") != task["run_id"]:
        raise TaskError("result run_id does not match the task")
    if result.get("revision") != task["expected_revision"]:
        raise TaskError("result revision does not match the task")
    required = task.get("required_outputs", [])
    if not isinstance(required, list) or not required:
        raise TaskError("task must declare required_outputs")
    for name in required:
        value = result["outputs"].get(name)
        if not isinstance(value, str) or not value.strip():
            raise TaskError(f"result outputs must contain non-empty {name}")
