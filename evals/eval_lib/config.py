"""Strict experiment, suite, and case configuration loading."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .records import EvalError


def _yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise EvalError(f"invalid YAML {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise EvalError(f"YAML root must be a mapping: {path}")
    return value


def _keys(value: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise EvalError(f"{label} has unknown keys: {', '.join(sorted(unknown))}")


def _path(base: Path, value: Any, field: str, *, require: bool = True) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise EvalError(f"{field} must be a non-empty path")
    candidate = Path(value)
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()
    if require and not resolved.exists():
        raise EvalError(f"{field} does not exist: {resolved}")
    return resolved


def _positive(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise EvalError(f"{field} must be a positive integer")
    return value


@dataclass(frozen=True)
class Candidate:
    name: str
    model: str
    accepted_resolved_models: tuple[str, ...]


@dataclass(frozen=True)
class Suite:
    path: Path
    id: str
    version: int
    target_worker: str
    checks: tuple[str, ...]
    dimensions: tuple[str, ...]


@dataclass(frozen=True)
class Case:
    path: Path
    id: str
    issue_key: str
    source_issue: Path
    judge_notes: Path
    judge_context_files: tuple[str, ...]


@dataclass(frozen=True)
class Experiment:
    path: Path
    name: str
    suite: Suite
    worker: str
    profile: str
    invocation: str
    candidates: dict[str, Candidate]
    cases: tuple[Case, ...]
    repetitions: int
    order: str
    worker_timeout_seconds: int
    startup_timeout_seconds: int
    container_image: str
    context_workspace: Path | None
    prepare_context: bool
    judge_model: str
    judge_accepted_models: tuple[str, ...]
    judge_prompt: Path
    judge_timeout_seconds: int
    evidence_max_bytes: int
    order_swap: bool


def load_suite(path: Path) -> Suite:
    value = _yaml(path)
    _keys(value, {"schema_version", "id", "version", "target_worker", "checks", "dimensions"}, "suite")
    if value.get("schema_version") != 1 or not isinstance(value.get("id"), str):
        raise EvalError("suite requires schema_version: 1 and id")
    checks = value.get("checks")
    dimensions = value.get("dimensions")
    if not isinstance(checks, list) or not checks or not all(isinstance(item, str) for item in checks):
        raise EvalError("suite.checks must be a non-empty string list")
    if not isinstance(dimensions, list) or not dimensions or not all(isinstance(item, str) for item in dimensions):
        raise EvalError("suite.dimensions must be a non-empty string list")
    return Suite(path, value["id"], _positive(value.get("version"), "suite.version"),
                 str(value.get("target_worker", "")), tuple(checks), tuple(dimensions))


def load_case(path: Path) -> Case:
    value = _yaml(path / "case.yaml")
    _keys(value, {"schema_version", "id", "issue_key", "source_issue", "judge_notes", "judge_context_files"}, "case")
    if value.get("schema_version") != 1 or not isinstance(value.get("id"), str) or not isinstance(value.get("issue_key"), str):
        raise EvalError(f"case {path} requires schema_version, id, and issue_key")
    files = value.get("judge_context_files", [])
    if not isinstance(files, list) or not all(isinstance(item, str) and item for item in files):
        raise EvalError(f"case {path} judge_context_files must be a string list")
    return Case(path.resolve(), value["id"], value["issue_key"],
                _path(path, value.get("source_issue"), "case.source_issue"),
                _path(path, value.get("judge_notes"), "case.judge_notes"), tuple(files))


def load_experiment(path: Path) -> Experiment:
    path = path.resolve()
    value = _yaml(path)
    _keys(value, {"schema_version", "name", "suite", "target", "candidates", "cases", "repetitions", "execution", "context", "judge"}, "experiment")
    if value.get("schema_version") != 1 or not isinstance(value.get("name"), str):
        raise EvalError("experiment requires schema_version: 1 and name")
    suite = load_suite(_path(path.parent, value.get("suite"), "experiment.suite"))
    target = value.get("target")
    if not isinstance(target, dict):
        raise EvalError("experiment.target must be a mapping")
    _keys(target, {"worker", "profile", "invocation"}, "experiment.target")
    worker = target.get("worker")
    if worker != suite.target_worker or worker != "skill:rhai-feature-refine-worker":
        raise EvalError("v1 supports only suite and target worker skill:rhai-feature-refine-worker")
    if target.get("invocation") != "controller-worker":
        raise EvalError("v1 supports only target.invocation: controller-worker")
    if not isinstance(target.get("profile"), str) or not target["profile"]:
        raise EvalError("target.profile must be a non-empty profile name")
    raw_candidates = value.get("candidates")
    if not isinstance(raw_candidates, dict) or set(raw_candidates) != {"A", "B"}:
        raise EvalError("experiment.candidates must contain exactly A and B")
    candidates: dict[str, Candidate] = {}
    for name, raw in raw_candidates.items():
        if not isinstance(raw, dict):
            raise EvalError(f"candidate {name} must be a mapping")
        _keys(raw, {"model", "accepted_resolved_models"}, f"candidate {name}")
        model = raw.get("model")
        models = raw.get("accepted_resolved_models")
        if not isinstance(model, str) or not model or "REPLACE_" in model:
            raise EvalError(f"candidate {name}.model must be configured")
        if not isinstance(models, list) or not models or not all(isinstance(item, str) and "REPLACE_" not in item for item in models):
            raise EvalError(f"candidate {name}.accepted_resolved_models must be configured")
        candidates[name] = Candidate(name, model, tuple(models))
    raw_cases = value.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvalError("experiment.cases must be a non-empty list")
    cases = tuple(load_case(_path(path.parent, item, "experiment.cases item")) for item in raw_cases)
    if len({case.id for case in cases}) != len(cases):
        raise EvalError("experiment case IDs must be unique")
    execution = value.get("execution", {})
    if not isinstance(execution, dict):
        raise EvalError("experiment.execution must be a mapping")
    _keys(execution, {"order", "worker_timeout_seconds", "startup_timeout_seconds", "container_image"}, "experiment.execution")
    order = execution.get("order")
    if order != "alternating":
        raise EvalError("v1 requires execution.order: alternating")
    image = execution.get("container_image")
    if not isinstance(image, str) or not image:
        raise EvalError("execution.container_image must be set")
    context = value.get("context")
    if not isinstance(context, dict):
        raise EvalError("experiment.context must be a mapping")
    _keys(context, {"prepared_workspace", "prepare_from_profile"}, "experiment.context")
    prepared = context.get("prepared_workspace")
    prepare_from_profile = bool(context.get("prepare_from_profile", False))
    if bool(prepared) == prepare_from_profile:
        raise EvalError("context requires exactly one of prepared_workspace or prepare_from_profile: true")
    workspace = _path(path.parent, prepared, "context.prepared_workspace") if prepared else None
    judge = value.get("judge")
    if not isinstance(judge, dict):
        raise EvalError("experiment.judge must be a mapping")
    _keys(judge, {"model", "accepted_resolved_models", "prompt", "timeout_seconds", "evidence_max_bytes", "order_swap"}, "experiment.judge")
    judge_model = judge.get("model")
    accepted = judge.get("accepted_resolved_models")
    if not isinstance(judge_model, str) or not judge_model or "REPLACE_" in judge_model:
        raise EvalError("judge.model must be configured")
    if not isinstance(accepted, list) or not accepted or not all(isinstance(item, str) and "REPLACE_" not in item for item in accepted):
        raise EvalError("judge.accepted_resolved_models must be configured")
    return Experiment(path, value["name"], suite, worker, target["profile"], target["invocation"], candidates, cases,
                      _positive(value.get("repetitions"), "experiment.repetitions"), order,
                      _positive(execution.get("worker_timeout_seconds"), "execution.worker_timeout_seconds"),
                      _positive(execution.get("startup_timeout_seconds"), "execution.startup_timeout_seconds"), image,
                      workspace, prepare_from_profile, judge_model, tuple(accepted),
                      _path(path.parent, judge.get("prompt"), "judge.prompt"),
                      _positive(judge.get("timeout_seconds"), "judge.timeout_seconds"),
                      _positive(judge.get("evidence_max_bytes"), "judge.evidence_max_bytes"),
                      judge.get("order_swap") is True)
