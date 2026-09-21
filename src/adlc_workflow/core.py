"""Deterministic ADLC core for request normalization and task servicing."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .publication import PublicationError, publish_run
from .admission import AdmissionError, admit_request, enforce_profile_permissions, load_policy
from .plugins import PluginError, activate_plugins
from .context import ContextError, prepare_context, selected_overlay_paths
from .artifacts import ArtifactLayout, ArtifactLayoutError
from .profiles import load_profile, refine_template_path, resolve_profile_path
from .refinement import RefinementError, assemble_feature_document, uses_feature_document_assembly
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
            configured_profile = resolve_profile_path(self.install_root, profile_path)
            request_kwargs = request.get("kwargs", {})
            if not isinstance(request_kwargs, dict):
                raise WorkflowError("request kwargs must be an object")
            requested_profile = request_kwargs.get("profile")
            if requested_profile is not None:
                request_profile = resolve_profile_path(self.install_root, requested_profile)
                if request_profile != configured_profile:
                    raise WorkflowError(
                        f"request profile {request_profile} conflicts with configured profile {configured_profile}"
                    )
            profile = load_profile(self.install_root, configured_profile)
            workflow_definition = profile.get("workflow")
            workflow_stages = workflow_definition.get("stages", []) if isinstance(workflow_definition, dict) else []
            if any(isinstance(candidate, dict) and candidate.get("id") == "refine"
                   for candidate in workflow_stages):
                refine_template_path(self.install_root, profile)
            admission = admit_request(
                request,
                load_policy(self.install_root / "config/launch-modes.yaml"),
            )
            enforce_profile_permissions(profile, admission)
            artifacts = ArtifactLayout(self.workspace_root, profile)
            normalized = normalize_request(request, default_profile=configured_profile)
            context_components = request_kwargs.get("context_components", []) if isinstance(request_kwargs, dict) else []
            if not isinstance(context_components, list) or not all(isinstance(value, str) for value in context_components):
                raise WorkflowError("kwargs.context_components must be a list of strings")
            context_manifest = prepare_context(
                self.install_root, self.workspace_root, profile, normalized.mode, context_components
            )
        except (AdmissionError, SelectionError, ValueError, ArtifactLayoutError, ContextError, WorkflowDefinitionError) as exc:
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
            "parents": kwargs.get("parent_issues", {}),
            "linked": {},
        }
        if not isinstance(gate_context["parents"], dict):
            raise WorkflowError("kwargs.parent_issues must be an object")
        for key, issue in source_issues.items():
            if not isinstance(issue, dict):
                continue
            fields = issue.get("fields", {})
            if not isinstance(fields, dict):
                fields = {}
            parent = gate_context["parents"].get(key, kwargs.get("parent_issue"))
            if parent is None:
                parent = fields.get("parent", issue.get("parent"))
            if parent is not None:
                gate_context["parents"][key] = parent
            links = fields.get("issuelinks", issue.get("issuelinks", []))
            linked = []
            if isinstance(links, list):
                for link in links:
                    if not isinstance(link, dict):
                        continue
                    linked_issue = link.get("inwardIssue") or link.get("outwardIssue")
                    if isinstance(linked_issue, dict):
                        linked.append(linked_issue)
            gate_context["linked"][key] = linked
        try:
            plugin_receipts = activate_plugins(self.install_root, profile, gate_context)
        except PluginError as exc:
            raise WorkflowError(str(exc)) from exc
        selection = freeze_selection(normalized)
        selection_metadata = kwargs.get("selection_metadata")
        if selection_metadata is not None:
            if not isinstance(selection_metadata, dict):
                raise WorkflowError("kwargs.selection_metadata must be an object")
            for key in ("selector", "source", "exclusion_reasons"):
                if key in selection_metadata:
                    selection[key] = selection_metadata[key]
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
        # A request describes the work, but it is not the identity of an
        # execution. Include a fresh invocation token so repeated runs with
        # identical input receive distinct state directories and publication
        # markers while task IDs remain deterministic within each run.
        run_id = f"run-{uuid.uuid4().hex[:16]}"
        if self.state.state_path(run_id).exists():
            raise WorkflowError(f"run already exists: {run_id}")
        selection["run_id"] = run_id
        selection["profile"] = configured_profile
        public_selection = artifacts.evidence_path("selection")
        self.state.write_json(public_selection, selection)
        request_path = self.state.request_path(run_id)
        self.state.write_json(request_path, request)
        items = [
            {"work_id": f"work-{_digest({'run': run_id, 'key': key})[:16]}", "key": key, "status": "pending", "stage": None}
            for key in selection["ordered_keys"]
        ]
        state = {
            "schema_version": 1,
            "run_id": run_id,
            "profile": configured_profile,
            "lifecycle": profile["lifecycle"],
            "mode": normalized.mode,
            "identity": normalized.identity,
            "admission": admission,
            "plugins": plugin_receipts,
            "request_path": str(request_path),
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
                task = self.state.read_json(
                    self.state.task_path(run_id, item["work_id"], item["task_id"])
                )
                return {"kind": "task", **task}
            profile = load_profile(self.install_root, state["profile"])
            artifacts = ArtifactLayout(self.workspace_root, profile)
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
                    state["publication"] = {"status": "running", "receipts": []}

                    def persist_receipt(receipt: dict[str, Any]) -> None:
                        state["publication"]["receipts"].append(receipt)
                        state["updated_at"] = _now()
                        self._write_state(state)

                    try:
                        publication = publish_run(
                            self.install_root, self.workspace_root, profile, state, run_id,
                            on_receipt=persist_receipt,
                        )
                    except (PublicationError, WorkflowDefinitionError) as exc:
                        item["status"] = "blocked"
                        state["status"] = "blocked"
                        state["blocked_reason"] = str(exc)
                        receipts = state.get("publication", {}).get("receipts", [])
                        state["publication"] = {"status": "failed", "error": str(exc), "receipts": receipts}
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
                "result_path": str(self.state.result_path(run_id, item["work_id"], task_id)),
                "allowed_outputs": stage_outputs(stage_definition),
                "required_outputs": stage_outputs(stage_definition),
                "worker": worker,
            }
            if uses_feature_document_assembly(stage_definition):
                task["fragment_path"] = str(
                    self.state.work_dir(run_id, item["work_id"]) / "fragments" / f"{task_id}.md"
                )
                task["worker_resources"] = self._feature_worker_resources(run_id, profile)
                self._clear_public_refinement_artifact(artifacts, item["key"])
            task_path = self.state.task_path(run_id, item["work_id"], task_id)
            if task_path.exists():
                task = self.state.read_json(task_path)
            else:
                self.state.write_json(task_path, task)
            item.update({"status": "waiting_for_result", "stage": stage, "task_id": task_id})
            state["updated_at"] = _now()
            self._write_state(state)
            return {"kind": "task", **task}

    def claim(self, run_id: str) -> dict[str, Any]:
        """Atomically claim one non-publication task for a parallel scheduler.

        This leaves the original ``advance`` protocol intact for agent-led and
        sequential callers. A claimed task is immediately persisted as
        ``waiting_for_result``, so a second scheduler cannot receive it.
        Publication is deliberately never claimed: it remains a batch barrier
        serviced by ``advance`` after every processing task has submitted.
        """
        with self.state.lock(run_id):
            state = self._read(run_id)
            if state["status"] == "complete":
                return {"kind": "complete", "run_id": run_id, "state": state}
            if state["status"] == "blocked":
                return {"kind": "blocked", "run_id": run_id, "reason": state["blocked_reason"]}
            profile = load_profile(self.install_root, state["profile"])
            for item in state["items"]:
                if item["status"] != "pending":
                    continue
                stage_definition = next_stage(profile, item["stage"])
                if stage_definition is None:
                    raise WorkflowError(f"workflow has no next stage after {item['stage']}")
                if stage_definition.get("kind") == "publication":
                    continue
                task = self._claim_task(state, item, stage_definition, profile)
                self._write_state(state)
                return {"kind": "task", **task}
            if any(item["status"] == "waiting_for_result" for item in state["items"]):
                return {"kind": "idle", "run_id": run_id}
            return {"kind": "publication_ready", "run_id": run_id}

    def _claim_task(
        self,
        state: dict[str, Any],
        item: dict[str, Any],
        stage_definition: dict[str, Any],
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one claimed processing task while the run lock is held."""
        run_id = state["run_id"]
        stage_id = stage_definition["id"]
        gate_failures = evaluate_gate(
            stage_definition,
            self._gate_context(state.get("gate_context", {}), item["key"]),
        )
        if gate_failures:
            state["status"] = "blocked"
            state["blocked_reason"] = f"stage {stage_id} gate failed: {'; '.join(gate_failures)}"
            state["updated_at"] = _now()
            self._write_state(state)
            raise WorkflowError(state["blocked_reason"])
        revision = state["revision"]
        task_id = f"task-{_digest({'run': run_id, 'work': item['work_id'], 'stage': stage_id, 'revision': revision})[:16]}"
        task = {
            "schema_version": 1,
            "task_id": task_id,
            "run_id": run_id,
            "work_id": item["work_id"],
            "issue_key": item["key"],
            "stage": stage_id,
            "expected_revision": revision,
            "result_path": str(self.state.result_path(run_id, item["work_id"], task_id)),
            "allowed_outputs": stage_outputs(stage_definition),
            "required_outputs": stage_outputs(stage_definition),
            "worker": stage_definition.get("worker"),
        }
        if uses_feature_document_assembly(stage_definition):
            task["fragment_path"] = str(
                self.state.work_dir(run_id, item["work_id"]) / "fragments" / f"{task_id}.md"
            )
            task["worker_resources"] = self._feature_worker_resources(run_id, profile)
            self._clear_public_refinement_artifact(ArtifactLayout(self.workspace_root, profile), item["key"])
        task_path = self.state.task_path(run_id, item["work_id"], task_id)
        if task_path.exists():
            task = self.state.read_json(task_path)
        else:
            self.state.write_json(task_path, task)
        item.update({"status": "waiting_for_result", "stage": stage_id, "task_id": task_id})
        state["updated_at"] = _now()
        return task

    def submit(self, run_id: str, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        with self.state.lock(run_id):
            state = self._read(run_id)
            item = next((candidate for candidate in state["items"] if candidate.get("task_id") == task_id), None)
            if item is None or item["status"] != "waiting_for_result":
                raise WorkflowError("task is not pending for this run")
            task = self.state.read_json(
                self.state.task_path(run_id, item["work_id"], task_id)
            )
            try:
                validate_result(task, result)
            except TaskError as exc:
                raise WorkflowError(str(exc)) from exc
            profile = load_profile(self.install_root, state["profile"])
            stage_definition = stage(profile, item["stage"])
            accepted_result = result
            if uses_feature_document_assembly(stage_definition):
                source = self._gate_context(state.get("gate_context", {}), item["key"]).get("source")
                try:
                    assembled = assemble_feature_document(
                        self.install_root, profile, source, result["outputs"]["strategy_markdown"],
                    )
                except RefinementError as exc:
                    raise WorkflowError(str(exc)) from exc
                accepted_result = {**result, "outputs": {**result["outputs"], "strategy_markdown": assembled}}
            artifacts = ArtifactLayout(self.workspace_root, profile)
            for output_name, artifact_name in stage_output_artifacts(stage_definition).items():
                content = accepted_result["outputs"].get(output_name)
                if not isinstance(content, str) or not content.strip():
                    continue
                if artifact_name == "task":
                    destination = artifacts.task(item["key"])
                elif artifact_name == "review":
                    destination = artifacts.review(item["key"])
                elif isinstance(artifact_name, str) and artifact_name.startswith("generated:"):
                    destination = artifacts.generated(artifact_name.removeprefix("generated:"), item["key"])
                else:
                    raise WorkflowError(f"unsupported output artifact target: {artifact_name}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            self.state.write_json(self.state.result_path(run_id, item["work_id"], task_id), accepted_result)
            item["status"] = "pending"
            item["result_path"] = str(self.state.result_path(run_id, item["work_id"], task_id))
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
    def _clear_public_refinement_artifact(artifacts: ArtifactLayout, issue_key: str) -> None:
        """Ensure a failed/new fragment can never leave a stale public strategy."""
        destination = artifacts.task(issue_key)
        if destination.exists() and not destination.is_file():
            raise WorkflowError(f"refinement artifact path is not a file: {destination}")
        destination.unlink(missing_ok=True)

    def _feature_worker_resources(self, run_id: str, profile: dict[str, Any]) -> dict[str, Any]:
        """Freeze the read-only inputs a feature worker may need."""
        manifest = self.workspace_root / ".context" / "context-manifest.json"
        return {
            "source_request_path": str(self.state.request_path(run_id)),
            "template_path": str(refine_template_path(self.install_root, profile)),
            "context_manifest_path": str(manifest),
            "overlay_paths": [str(path) for path in selected_overlay_paths(self.workspace_root)],
        }

    @staticmethod
    def _gate_context(context: dict[str, Any], issue_key: str) -> dict[str, Any]:
        sources = context.get("sources", {})
        if not isinstance(sources, dict):
            sources = {}
        # Read old single-source state during the transition to batch context.
        source = sources.get(issue_key, context.get("source"))
        parents = context.get("parents", {})
        parent = parents.get(issue_key) if isinstance(parents, dict) else None
        if parent is None:
            parent = context.get("parent")
        linked = context.get("linked", {})
        linked_issues = linked.get(issue_key, []) if isinstance(linked, dict) else []
        return {"source": source, "parent": parent, "linked": linked_issues}
