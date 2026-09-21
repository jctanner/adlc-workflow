"""Narrow adapter to the frozen production worker implementation."""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .records import EvalError, sha256_bytes


def _load(plugin_root: Path) -> dict[str, Any]:
    source = str((plugin_root / "src").resolve())
    if source not in sys.path:
        sys.path.insert(0, source)
    try:
        artifacts = importlib.import_module("adlc_workflow.artifacts")
        controller = importlib.import_module("adlc_workflow.controller")
        profiles = importlib.import_module("adlc_workflow.profiles")
        workflow = importlib.import_module("adlc_workflow.workflow")
        refinement = importlib.import_module("adlc_workflow.refinement")
    except ImportError as exc:
        raise EvalError(f"frozen plugin lacks required production bridge API: {exc}") from exc
    required = ("ArtifactLayout", "ClaudeWorkerRuntime", "RunReporter", "_skill_prompt", "_validate_worker_completion",
                "resolve_profile_path", "load_profile", "refine_template_path", "stage", "stage_outputs",
                "assemble_feature_document")
    modules = (artifacts, controller, profiles, workflow, refinement)
    values = {name: next((getattr(module, name) for module in modules if hasattr(module, name)), None) for name in required}
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise EvalError(f"frozen plugin bridge API changed; missing {', '.join(missing)}")
    return values


def build_task(plugin_root: Path, workspace: Path, profile_name: str, issue_key: str) -> tuple[dict[str, Any], str, Path]:
    api = _load(plugin_root)
    profile_path = api["resolve_profile_path"](plugin_root, profile_name)
    profile = api["load_profile"](plugin_root, profile_path)
    definition = api["stage"](profile, "refine")
    if definition.get("worker") != "skill:rhai-feature-refine-worker":
        raise EvalError("profile refine stage is not the supported worker")
    output_names = api["stage_outputs"](definition)
    if output_names != ["strategy_markdown"]:
        raise EvalError(f"profile refine outputs are unsupported: {output_names}")
    layout = api["ArtifactLayout"](workspace, profile)
    template = api["refine_template_path"](plugin_root, profile)
    try:
        template_relative = template.relative_to(plugin_root)
    except ValueError as exc:
        raise EvalError(f"refine template escapes frozen plugin: {template}") from exc
    task = {
        "schema_version": 1, "task_id": f"eval-refine-{issue_key.lower()}", "run_id": "eval-worker",
        "work_id": f"eval-{issue_key.lower()}", "issue_key": issue_key, "stage": "refine",
        "expected_revision": 0, "result_path": "/workspace/.adlc-eval/unused-result.json",
        "allowed_outputs": output_names, "required_outputs": output_names,
        "worker": definition["worker"],
        "fragment_path": f"/workspace/.adlc-eval/fragments/{issue_key}.md",
        "worker_resources": {
            "source_request_path": "/workspace/.adlc-eval/input/request.json",
            "template_path": f"/home/evaluator/.claude/plugins/adlc-workflow/{template_relative.as_posix()}",
            "context_manifest_path": "/workspace/.context/context-manifest.json",
            "overlay_paths": [],
        },
    }
    return task, profile_path, layout.task(issue_key)


def execute_worker(plugin_root: Path, workspace: Path, input_dir: Path, model: str, timeout_seconds: int) -> dict[str, Any]:
    """Run one production-style worker and retain its full local evidence."""
    api = _load(plugin_root)
    task_path = input_dir / "task.json"
    request_path = input_dir / "request.json"
    if not task_path.is_file() or not request_path.is_file():
        raise EvalError("worker input requires task.json and request.json")
    try:
        task = json.loads(task_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise EvalError(f"invalid worker task: {exc}") from exc
    if not isinstance(task, dict) or task.get("worker") != "skill:rhai-feature-refine-worker":
        raise EvalError("worker task is not a supported refinement task")
    profile_path = str(task.get("profile_path", "config/rhai-feature-creator.yaml"))
    profile_path = api["resolve_profile_path"](plugin_root, profile_path)
    profile = api["load_profile"](plugin_root, profile_path)
    layout = api["ArtifactLayout"](workspace, profile)
    output = layout.task(str(task["issue_key"]))
    fragment_value = task.get("fragment_path")
    if not isinstance(fragment_value, str) or not fragment_value:
        raise EvalError("feature refinement task has no fragment_path")
    output_paths = {"strategy_markdown": Path(fragment_value)}
    fragment = next(iter(output_paths.values()))
    if output.exists():
        raise EvalError(f"trial output already exists: {output}")
    definition = plugin_root / "skills" / "rhai-feature-refine-worker" / "SKILL.md"
    if not definition.is_file():
        raise EvalError(f"worker definition is missing: {definition}")
    attempt = workspace / ".adlc-eval" / "attempt"
    evidence = workspace / ".adlc-eval"
    evidence.mkdir(parents=True, exist_ok=True)
    prompt = api["_skill_prompt"](definition, task, output_paths, plugin_root, workspace, profile_path, request_path)
    (evidence / "prompt.txt").write_text(prompt, encoding="utf-8")
    reporter = api["RunReporter"]("json")
    reporter.bind_run(evidence / "events.jsonl")
    runtime = api["ClaudeWorkerRuntime"](
        plugin_root, workspace, profile_path, claude_bin=os.environ.get("ADLC_EVAL_CLAUDE_BIN", "claude"), model=model,
        timeout_seconds=timeout_seconds, dangerously_skip_permissions=True, reporter=reporter,
    )
    record: dict[str, Any] = {"schema_version": 1, "worker": task["worker"], "model_requested": model,
                              "prompt_sha256": sha256_bytes(prompt.encode()), "output_path": str(output)}
    try:
        completion = runtime.run(worker_name="rhai-feature-refine-worker", prompt=prompt, attempt_dir=attempt,
                                 event_context={"case_id": task.get("case_id"), "candidate": task.get("candidate")})
        (evidence / "completion.txt").write_text(completion, encoding="utf-8")
        api["_validate_worker_completion"](completion, task, output_paths, "rhai-feature-refine-worker")
        if not fragment.is_file() or not fragment.read_text(encoding="utf-8").strip():
            raise EvalError("worker completion did not create a non-empty strategy fragment")
        request = json.loads(request_path.read_text(encoding="utf-8"))
        source = request.get("kwargs", {}).get("source_issues", {}).get(task["issue_key"])
        if not isinstance(source, dict):
            raise EvalError("captured source request has no source issue for feature assembly")
        assembled = api["assemble_feature_document"](plugin_root, profile, source, fragment.read_text(encoding="utf-8"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(assembled, encoding="utf-8")
        record.update({"status": "succeeded", "artifact_sha256": sha256_bytes(output.read_bytes()),
                       "artifact_bytes": output.stat().st_size})
    except BaseException as exc:
        record.update({"status": "failed", "error": str(exc), "error_type": type(exc).__name__})
    (evidence / "worker-result.json").write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record
