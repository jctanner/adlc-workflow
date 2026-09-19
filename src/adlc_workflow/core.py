"""Deterministic ADLC core for request normalization and task servicing."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .publication import PublicationError, publish_run
from .context import ContextError, prepare_context
from .artifacts import ArtifactLayout, ArtifactLayoutError
from .profiles import load_profile
from .selection import SelectionError, freeze_selection, normalize_request
from .state import StateError, StateStore
from .tasks import TaskError, validate_result
from .workflow import WorkflowDefinitionError, evaluate_gate, next_stage, stage, stage_output_artifacts, stage_outputs, stages


class WorkflowError(RuntimeError):
    """Raised when a core operation violates the workflow contract."""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


class WorkflowCore:
    """Owns run state and emits bounded semantic tasks."""

    def __init__(
        self,
        install_root: str | Path,
        workspace_root: str | Path | None = None,
        state_root: str | Path | None = None,
    ):
        """Create a core bound to immutable install resources and a runtime workspace.

        ``workspace_root`` defaults to ``install_root`` for compatibility with
        the original single-root API.
        """
        # Keep the lexical install path intact. Claude's supported local
        # marketplace link mode creates symlinks inside the cache back to the
        # mounted plugin, so resolving here makes an in-cache profile appear
        # to escape its install root.
        self.install_root = Path(install_root).absolute()
        self.workspace_root = Path(workspace_root or install_root).resolve()
        self.state = StateStore(state_root or self.workspace_root / ".adlc" / "state")

    def start(self, request: dict[str, Any], profile_path: str = "config/rhai-feature-creator.yaml") -> dict[str, Any]:
        try:
            profile = load_profile(self.install_root, profile_path)
            artifacts = ArtifactLayout(self.workspace_root, profile)
            normalized = normalize_request(request, default_profile=profile_path)
            context_manifest = prepare_context(self.install_root, self.workspace_root, profile, normalized.mode)
        except (SelectionError, ValueError, ArtifactLayoutError, ContextError, WorkflowDefinitionError) as exc:
            raise WorkflowError(str(exc)) from exc
        kwargs = request.get("kwargs", {})
        source_issues = kwargs.get("source_issues", {})
        if not isinstance(source_issues, dict):
            raise WorkflowError("kwargs.source_issues must be an object")
        if not source_issues and kwargs.get("source_issue") is not None:
            source_issues = {key: kwargs.get("source_issue") for key in normalized.args}
        gate_context = {
            "sources": source_issues,
            "parent": kwargs.get("parent_issue"),
        }
        selection = freeze_selection(normalized)
        first_stage = next_stage(profile, None)
        if first_stage is None:
            raise WorkflowError("profile workflow has no stages")
        if selection["ordered_keys"]:
            failures = []
            for key in selection["ordered_keys"]:
                failures.extend(
                    f"{key}: {failure}"
                    for failure in evaluate_gate(first_stage, self._gate_context(gate_context, key))
                )
            if failures:
                raise WorkflowError(f"stage {first_stage['id']} gate failed: {'; '.join(failures)}")
        basis = {
            "args": list(normalized.args),
            "operation": normalized.operation,
            "mode": normalized.mode,
            "identity": normalized.identity,
            "profile": profile_path,
            "selection": selection,
        }
        run_id = f"run-{_digest(basis)[:16]}"
        if self.state.state_path(run_id).exists():
            raise WorkflowError(f"run already exists: {run_id}")
        selection["run_id"] = run_id
        selection["profile"] = profile_path
        public_selection = artifacts.evidence_path("selection")
        self.state.write_json(public_selection, selection)
        items = [
            {"work_id": f"work-{_digest({'run': run_id, 'key': key})[:16]}", "key": key, "status": "pending", "stage": None}
            for key in selection["ordered_keys"]
        ]
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "profile": profile_path,
            "lifecycle": profile["lifecycle"],
            "mode": normalized.mode,
            "identity": normalized.identity,
            "request": request,
            "selection_path": str(public_selection.relative_to(self.workspace_root)),
            "context": context_manifest,
            "gate_context": gate_context,
            "items": items,
            "current_index": 0,
            "revision": 0,
            "status": "complete" if not items else "running",
            "created_at": _now(),
            "updated_at": _now(),
        }
        context_record = artifacts.root("inputs") / "context-manifest.json"
        self.state.write_json(context_record, context_manifest)
        with self.state.lock(run_id):
            self.state.write_json(self.state.state_path(run_id), state)
        return self.status(run_id)

    def status(self, run_id: str) -> dict[str, Any]:
        try:
            return self.state.read_json(self.state.state_path(run_id))
        except StateError as exc:
            raise WorkflowError(str(exc)) from exc

    def advance(self, run_id: str) -> dict[str, Any]:
        with self.state.lock(run_id):
            state = self._read(run_id)
            if state["status"] == "complete":
                return {"kind": "complete", "run_id": run_id, "state": state}
            if state["status"] == "blocked":
                return {"kind": "blocked", "run_id": run_id, "reason": state["blocked_reason"]}
            item = state["items"][state["current_index"]]
            if item["status"] == "waiting_for_result":
                task = self.state.read_json(self.state.task_path(run_id, item["task_id"]))
                return {"kind": "task", **task}
            profile = load_profile(self.install_root, state["profile"])
            stage_definition = next_stage(profile, item["stage"])
            if stage_definition is None:
                raise WorkflowError(f"workflow has no next stage after {item['stage']}")
            stage = stage_definition["id"]
            if stage_definition.get("kind") == "publication":
                processing_stages = [candidate for candidate in stages(profile) if candidate.get("kind") != "publication"]
                final_processing_stage = processing_stages[-1]["id"] if processing_stages else None
                batch_ready = all(
                    candidate["status"] == "pending" and candidate.get("stage") == final_processing_stage
                    for candidate in state["items"]
                )
                if not batch_ready:
                    next_index = next(
                        (
                            index
                            for index, candidate in enumerate(state["items"])
                            if candidate["status"] == "pending" and candidate.get("stage") != final_processing_stage
                        ),
                        None,
                    )
                    if next_index is None:
                        raise WorkflowError("batch is not publication-ready")
                    state["current_index"] = next_index
                    item = state["items"][next_index]
                    stage_definition = next_stage(profile, item["stage"])
                    if stage_definition is None or stage_definition.get("kind") == "publication":
                        raise WorkflowError("batch could not advance to a pre-publication stage")
                    stage = stage_definition["id"]
                else:
                    item["stage"] = stage
                    try:
                        publication = publish_run(self.install_root, self.workspace_root, profile, state, run_id)
                    except (PublicationError, WorkflowDefinitionError) as exc:
                        item["status"] = "blocked"
                        state["status"] = "blocked"
                        state["blocked_reason"] = str(exc)
                        state["publication"] = {"status": "failed", "error": str(exc)}
                        self._write_state(state)
                        return {"kind": "blocked", "run_id": run_id, "reason": state["blocked_reason"]}
                    for candidate in state["items"]:
                        candidate["stage"] = stage
                        candidate["status"] = "published"
                    state["status"] = "complete"
                    state["publication"] = publication
                    state["updated_at"] = _now()
                    self._write_state(state)
                    return {"kind": "complete", "run_id": run_id, "publication": publication}
            revision = state["revision"]
            task_id = f"task-{_digest({'run': run_id, 'work': item['work_id'], 'stage': stage, 'revision': revision})[:16]}"
            gate_failures = evaluate_gate(
                stage_definition,
                self._gate_context(state.get("gate_context", {}), item["key"]),
            )
            if gate_failures:
                state["status"] = "blocked"
                state["blocked_reason"] = f"stage {stage} gate failed: {'; '.join(gate_failures)}"
                state["updated_at"] = _now()
                self._write_state(state)
                return {"kind": "blocked", "run_id": run_id, "reason": state["blocked_reason"]}
            worker = stage_definition.get("worker")
            task = {
                "schema_version": 1,
                "task_id": task_id,
                "run_id": run_id,
                "work_id": item["work_id"],
                "issue_key": item["key"],
                "stage": stage,
                "expected_revision": revision,
                "result_path": str(self.state.result_path(run_id, task_id)),
                "allowed_outputs": stage_outputs(stage_definition),
                "required_outputs": stage_outputs(stage_definition),
                "worker": worker,
            }
            task_path = self.state.task_path(run_id, task_id)
            if task_path.exists():
                task = self.state.read_json(task_path)
            else:
                self.state.write_json(task_path, task)
            item.update({"status": "waiting_for_result", "stage": stage, "task_id": task_id})
            state["updated_at"] = _now()
            self._write_state(state)
            return {"kind": "task", **task}

    def submit(self, run_id: str, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock(run_id):
            state = self._read(run_id)
            task = self.state.read_json(self.state.task_path(run_id, task_id))
            item = next((candidate for candidate in state["items"] if candidate.get("task_id") == task_id), None)
            if item is None or item["status"] != "waiting_for_result":
                raise WorkflowError("task is not pending for this run")
            try:
                validate_result(task, result)
            except TaskError as exc:
                raise WorkflowError(str(exc)) from exc
            profile = load_profile(self.install_root, state["profile"])
            artifacts = ArtifactLayout(self.workspace_root, profile)
            for output_name, artifact_name in stage_output_artifacts(stage(profile, item["stage"])).items():
                content = result["outputs"].get(output_name)
                if not isinstance(content, str) or not content.strip():
                    continue
                if artifact_name == "task":
                    destination = artifacts.task(item["key"])
                elif artifact_name == "review":
                    destination = artifacts.review(item["key"])
                else:
                    raise WorkflowError(f"unsupported output artifact target: {artifact_name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            self.state.write_json(self.state.result_path(run_id, task_id), result)
            item["status"] = "pending"
            item["result_path"] = str(self.state.result_path(run_id, task_id))
            state["revision"] += 1
            state["updated_at"] = _now()
            self._write_state(state)
        return self.status(run_id)

    def _read(self, run_id: str) -> dict[str, Any]:
        try:
            return self.state.read_json(self.state.state_path(run_id))
        except StateError as exc:
            raise WorkflowError(str(exc)) from exc

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state.write_json(self.state.state_path(state["run_id"]), state)

    @staticmethod
    def _gate_context(context: dict[str, Any], issue_key: str) -> dict[str, Any]:
        sources = context.get("sources", {})
        if not isinstance(sources, dict):
            sources = {}
        # Read old single-source state during the transition to batch context.
        source = sources.get(issue_key, context.get("source"))
        return {"source": source, "parent": context.get("parent")}
