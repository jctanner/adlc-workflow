"""Deterministic handoff controller for profile-defined ADLC workflows."""

from __future__ import annotations

import concurrent.futures
import json
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .adapters.jira import JiraAdapterError, JiraClient
from .aggregation import AggregationError, aggregate_review
from .artifacts import ArtifactLayout, ArtifactLayoutError
from .core import WorkflowCore, WorkflowError
from .profiles import ProfileError, load_profile, resolve_profile_path
from .reviews import review_plan
from .refinement import uses_feature_document_assembly
from .scoring import ScoringError, score_review
from .selection import KEY_RE
from .state import StateError
from .workflow import WorkflowDefinitionError, evaluate_gate, next_stage, stage


class ControllerError(RuntimeError):
    """Raised when deterministic handoff orchestration cannot proceed."""


@dataclass(frozen=True)
class ControllerOptions:
    profile: str = "rhai-feature-creator"
    issue_keys: tuple[str, ...] = ()
    mode: str = "local"
    identity: str = "adlc-handoff"
    model: str | None = None
    timeout_seconds: float | None = None
    reviewer_parallelism: int | None = None
    max_attempts: int | None = None
    item_parallelism: int = 1
    batch_size: int | None = None
    batch_offset: int = 0
    state_root: str | Path | None = None
    claude_bin: str = "claude"
    dangerously_skip_permissions: bool = False
    output: str = "human"


class RunReporter:
    """Emit a controller-owned, causally ordered run event stream.

    JSON mode is a machine protocol: stdout contains exactly one JSON object
    per line. Human mode renders the same events for a terminal while keeping
    child stderr on stderr. In both cases a private events.jsonl is retained
    once the core allocates a run ID.
    """

    def __init__(self, mode: str):
        if mode not in {"human", "json"}:
            raise ControllerError("controller output must be human or json")
        self.mode = mode
        self._lock = threading.Lock()
        self._sequence = 0
        self._event_path: Path | None = None

    def bind_run(self, path: Path) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._event_path = path

    def emit(self, event_type: str, **fields: Any) -> None:
        with self._lock:
            self._sequence += 1
            event = {
                "type": event_type,
                "timestamp": time.time(),
                "sequence": self._sequence,
                **{key: value for key, value in fields.items() if value is not None},
            }
            encoded = json.dumps(event, sort_keys=True, separators=(",", ":"))
            if self._event_path is not None:
                with self._event_path.open("a", encoding="utf-8") as handle:
                    handle.write(encoded + "\n")
            if self.mode == "json":
                print(encoded, flush=True)
            else:
                self._render_human(event)

    def child_stderr(self, text: str, **context: Any) -> None:
        self.emit("claude.stderr", text=text, **context)
        if self.mode == "human":
            worker = context.get("worker", "claude")
            print(f"[{worker} stderr] {text}", file=sys.stderr, flush=True)

    def _render_human(self, event: dict[str, Any]) -> None:
        event_type = event["type"]
        if event_type.startswith("controller."):
            name = event_type.removeprefix("controller.")
            if name.startswith("worker_"):
                detail = event.get("worker", "unknown worker")
                if event.get("status"):
                    detail += f" ({event['status']})"
            elif name == "publication_receipt":
                detail = event.get("receipt", {}).get("issue_key") or event.get("receipt", {}).get("manifest") or "published"
            elif name == "usage_summary":
                usage = event.get("usage", {})
                detail = (
                    f"workers={usage.get('workers', 0)}, "
                    f"cost=${usage.get('cost_usd', 0):.4f}, "
                    f"input={usage.get('input_tokens', 0)}, "
                    f"output={usage.get('output_tokens', 0)}, "
                    f"cache-read={usage.get('cache_read_input_tokens', 0)}"
                )
            elif name == "review_verdict":
                review = event.get("review", {})
                detail = (
                    f"{review.get('verdict', 'unknown')} "
                    f"({review.get('total', '?')}/{review.get('maximum', '?')}, "
                    f"zero-count={review.get('zero_count', '?')})"
                )
            elif name == "run_finished":
                detail = str(event.get("status", "complete"))
                duration = event.get("duration_seconds")
                if isinstance(duration, (int, float)):
                    detail += f" (duration={duration:.1f}s)"
            else:
                detail = event.get("status") or event.get("stage") or event.get("reason") or ""
            print(f"[controller] {name}{(': ' + str(detail)) if detail else ''}", flush=True)
            return
        if event_type != "claude.stream":
            return
        worker = event.get("worker", "claude")
        raw = event.get("event", {})
        if not isinstance(raw, dict):
            return
        raw_type = raw.get("type")
        if raw_type == "assistant":
            for block in raw.get("message", {}).get("content", []):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    print(f"[{worker}] {block['text'].strip()}", flush=True)
                elif block.get("type") == "tool_use":
                    name = block.get("name", "unknown")
                    detail = _tool_detail(name, block.get("input"))
                    print(f"[{worker} tool] {name}{(': ' + detail) if detail else ''}", flush=True)
        elif raw_type == "system" and raw.get("subtype") == "init":
            print(f"[{worker}] session started", flush=True)
        elif raw_type == "result":
            status = "error" if raw.get("is_error") else "complete"
            print(f"[{worker}] session {status}", flush=True)


class WorkerRuntime(Protocol):
    """Execution boundary for one bounded LLM worker invocation."""

    def run(self, *, worker_name: str, prompt: str, attempt_dir: Path,
            event_context: dict[str, Any]) -> str:
        """Run a worker and return its terminal result text."""


class ClaudeWorkerRuntime:
    """Invoke the Claude CLI without making its stream part of the protocol."""

    def __init__(
        self,
        install_root: Path,
        workspace_root: Path,
        profile_path: str,
        *,
        claude_bin: str,
        model: str | None,
        timeout_seconds: float,
        dangerously_skip_permissions: bool,
        reporter: RunReporter,
    ):
        self.install_root = install_root.absolute()
        self.workspace_root = workspace_root.resolve()
        self.profile_path = profile_path
        self.claude_bin = claude_bin
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.dangerously_skip_permissions = dangerously_skip_permissions
        self.reporter = reporter

    def run(self, *, worker_name: str, prompt: str, attempt_dir: Path,
            event_context: dict[str, Any]) -> str:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        command = [self.claude_bin]
        if self.dangerously_skip_permissions:
            command.append("--dangerously-skip-permissions")
        if self.model:
            command.extend(["--model", self.model])
        if worker_name == "rhai-feature-refine-worker":
            # This worker needs only frozen documents plus its private output.
            # Restrict its tool surface instead of trusting it not to use Jira.
            command.extend(["--tools", "Read,Write"])
        command.extend(["--output-format", "stream-json"])
        # The machine protocol retains every partial delta. Human mode uses
        # Claude's complete assistant/tool records to avoid fragmented text.
        if self.reporter.mode == "json":
            command.append("--include-partial-messages")
        command.extend(["--verbose", "-p", prompt])
        environment = os.environ.copy()
        environment.update({
            "CLAUDE_PLUGIN_ROOT": str(self.install_root),
            "ADLC_WORKSPACE": str(self.workspace_root),
            "ADLC_PROFILE": self.profile_path,
        })
        command_path = attempt_dir / "command.json"
        command_path.write_text(json.dumps({
            "worker": worker_name,
            "argv": command[:-1] + ["<prompt>"],
            "cwd": str(self.workspace_root),
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        try:
            process = subprocess.Popen(
                command,
                cwd=self.workspace_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            raise ControllerError(f"could not start {worker_name}: {exc}") from exc
        self.reporter.emit("controller.worker_started", worker=worker_name,
                           attempt_dir=str(attempt_dir), **event_context)
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        stdout_path = attempt_dir / "stdout.jsonl"
        stderr_path = attempt_dir / "stderr.log"

        def read_stdout() -> None:
            assert process.stdout is not None
            with stdout_path.open("w", encoding="utf-8") as handle:
                for line in process.stdout:
                    stdout_lines.append(line)
                    handle.write(line)
                    handle.flush()
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        self.reporter.emit("claude.stdout", text=line.rstrip("\r\n"), worker=worker_name, **event_context)
                    else:
                        self.reporter.emit("claude.stream", event=event, worker=worker_name, **event_context)

        def read_stderr() -> None:
            assert process.stderr is not None
            with stderr_path.open("w", encoding="utf-8") as handle:
                for line in process.stderr:
                    stderr_lines.append(line)
                    handle.write(line)
                    handle.flush()
                    self.reporter.child_stderr(line.rstrip("\r\n"), worker=worker_name, **event_context)

        stdout_thread = threading.Thread(target=read_stdout, name=f"{worker_name}-stdout", daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, name=f"{worker_name}-stderr", daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        try:
            process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            _terminate_process_group(process)
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            raise ControllerError(f"{worker_name} exceeded {self.timeout_seconds:g}s") from exc
        except KeyboardInterrupt:
            _terminate_process_group(process)
            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
            raise
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        stdout = "".join(stdout_lines)
        (attempt_dir / "exit.json").write_text(json.dumps({
            "returncode": process.returncode,
            "finished_at": time.time(),
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if process.returncode:
            self.reporter.emit("controller.worker_finished", worker=worker_name, status="failed",
                               returncode=process.returncode, **event_context)
            raise ControllerError(
                f"{worker_name} exited {process.returncode}; inspect {attempt_dir / 'stderr.log'}"
            )
        result = _stream_result(stdout, worker_name, attempt_dir)
        self.reporter.emit("controller.worker_finished", worker=worker_name, status="complete",
                           returncode=process.returncode, **event_context)
        return result


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _tool_detail(name: Any, value: Any) -> str:
    """Render the useful, non-verbose portion of an agent tool invocation."""
    if not isinstance(value, dict):
        return ""
    if name == "Bash":
        return _compact_text(value.get("command"), prefix="$ ")
    if name in {"Read", "Write", "Edit"}:
        path = value.get("file_path")
        if not isinstance(path, str):
            return ""
        location = path
        if isinstance(value.get("offset"), int):
            location += f":{value['offset']}"
        return _compact_text(location)
    if name in {"Glob", "Grep"}:
        pattern = value.get("pattern")
        path = value.get("path")
        detail = str(pattern) if isinstance(pattern, str) else ""
        if isinstance(path, str):
            detail += f" in {path}" if detail else path
        return _compact_text(detail)
    return ""


def _compact_text(value: Any, *, prefix: str = "", limit: int = 240) -> str:
    if not isinstance(value, str):
        return ""
    text = prefix + " ".join(value.split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate a worker session and all of its child processes."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _stream_result(stdout: str, worker_name: str, attempt_dir: Path) -> str:
    """Return the successful terminal result from a stream-json transcript."""
    result: str | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "result":
            if event.get("is_error"):
                raise ControllerError(f"{worker_name} reported an error; inspect {attempt_dir / 'stdout.jsonl'}")
            candidate = event.get("result")
            if isinstance(candidate, str):
                result = candidate
    if result is None:
        raise ControllerError(
            f"{worker_name} did not emit a completed Claude stream; inspect {attempt_dir / 'stdout.jsonl'}"
        )
    return result


def _controller_settings(profile: dict[str, Any], options: ControllerOptions) -> dict[str, Any]:
    configured = profile.get("controller", {})
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise ControllerError("profile controller must be a mapping")
    def setting(name: str, supplied: Any, fallback: Any) -> Any:
        return supplied if supplied is not None else configured.get(name, fallback)
    timeout = setting("task_timeout_seconds", options.timeout_seconds, 900)
    parallelism = setting("reviewer_parallelism", options.reviewer_parallelism, 1)
    attempts = setting("max_attempts", options.max_attempts, 1)
    model = setting("model", options.model or os.environ.get("ADLC_CLAUDE_MODEL"), None)
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
        raise ControllerError("controller task_timeout_seconds must be positive")
    if not isinstance(parallelism, int) or isinstance(parallelism, bool) or parallelism <= 0:
        raise ControllerError("controller reviewer_parallelism must be a positive integer")
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts <= 0:
        raise ControllerError("controller max_attempts must be a positive integer")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ControllerError("controller model must be a non-empty string")
    return {"model": model, "timeout_seconds": float(timeout), "parallelism": parallelism, "max_attempts": attempts}


def _issue_context(issue: dict[str, Any]) -> dict[str, Any]:
    fields = issue.get("fields", {}) if isinstance(issue.get("fields"), dict) else {}
    parent = fields.get("parent", issue.get("parent"))
    linked = []
    for link in fields.get("issuelinks", issue.get("issuelinks", [])) or []:
        if isinstance(link, dict):
            candidate = link.get("inwardIssue") or link.get("outwardIssue")
            if isinstance(candidate, dict):
                linked.append(candidate)
    return {"source": issue, "parent": parent, "linked": linked}


def _initial_gate_jql(profile: dict[str, Any]) -> str:
    first = next_stage(profile, None)
    if first is None:
        raise ControllerError("profile workflow has no stages")
    gate = first.get("gate", {})
    source = gate.get("source") if isinstance(gate, dict) else None
    if not isinstance(source, dict):
        raise ControllerError("profile-only selection requires an initial source gate")
    project = source.get("project")
    if isinstance(project, str) and project:
        projects = [project]
    elif isinstance(project, list) and project and all(isinstance(value, str) and value for value in project):
        projects = project
    else:
        raise ControllerError("profile-only selection requires gate.source.project for a bounded Jira query")
    project_clause = "project = " + _jql_quote(projects[0]) if len(projects) == 1 else "project IN (" + ", ".join(_jql_quote(value) for value in projects) + ")"
    issue_type = source.get("issue_type")
    clauses = [project_clause]
    if isinstance(issue_type, str) and issue_type:
        clauses.append("issuetype = " + _jql_quote(issue_type))
    elif isinstance(issue_type, list) and issue_type and all(isinstance(value, str) and value for value in issue_type):
        clauses.append("issuetype IN (" + ", ".join(_jql_quote(value) for value in issue_type) + ")")
    return " AND ".join(clauses)


def _jql_quote(value: str) -> str:
    return '"' + value.replace('"', '\\"') + '"'


class HandoffController:
    """Drive a single workspace through the existing core task protocol."""

    def __init__(
        self,
        install_root: str | Path,
        workspace_root: str | Path,
        options: ControllerOptions,
        *,
        jira: JiraClient | None = None,
        workers: WorkerRuntime | None = None,
    ):
        self.install_root = Path(install_root).absolute()
        self.workspace_root = Path(workspace_root).resolve()
        self.options = options
        self.profile_path = resolve_profile_path(self.install_root, options.profile)
        self.profile = load_profile(self.install_root, self.profile_path)
        self.settings = _controller_settings(self.profile, options)
        if (not isinstance(options.item_parallelism, int) or isinstance(options.item_parallelism, bool)
                or options.item_parallelism < 1):
            raise ControllerError("item_parallelism must be a positive integer")
        self.core = WorkflowCore(self.install_root, self.workspace_root, options.state_root)
        self.reporter = RunReporter(options.output)
        self.jira = jira
        self._started_monotonic: float | None = None
        self.workers = workers or ClaudeWorkerRuntime(
            self.install_root, self.workspace_root, self.profile_path,
            claude_bin=options.claude_bin,
            model=self.settings["model"],
            timeout_seconds=self.settings["timeout_seconds"],
            dangerously_skip_permissions=options.dangerously_skip_permissions,
            reporter=self.reporter,
        )

    def run(self) -> dict[str, Any]:
        self._started_monotonic = time.monotonic()
        try:
            with self.core.state.execution_lease():
                self.reporter.emit("controller.selection_started", profile=self.profile_path)
                selection = self._select()
                self.reporter.emit("controller.selection_finished", profile=self.profile_path,
                                   status="no_work" if not selection["keys"] else "selected",
                                   issue_keys=selection["keys"], selection=selection["evidence"])
                if not selection["keys"]:
                    result = {
                        "kind": "complete", "status": "no_work", "profile": self.profile_path,
                        "selection": selection["evidence"], "duration_seconds": self._elapsed_seconds(),
                    }
                    self.reporter.emit("controller.run_finished", status="no_work", result=result,
                                       duration_seconds=result["duration_seconds"])
                    return result
                request = {
                    "args": selection["keys"],
                    "kwargs": {
                        "operation": "create",
                        "mode": self.options.mode,
                        "identity": self.options.identity,
                        "profile": self.profile_path,
                        "source_issues": selection["issues"],
                        "selection_metadata": selection["evidence"],
                    },
                }
                started = self.core.start(request, self.profile_path)
                run_id = started["run_id"]
                self.reporter.bind_run(self.core.state.run_dir(run_id) / "events.jsonl")
                self.reporter.emit("controller.run_started", run_id=run_id, profile=self.profile_path,
                                   issue_keys=selection["keys"], settings=self.settings,
                                   item_parallelism=self.options.item_parallelism)
                return self._drive(run_id)
        except ControllerError as exc:
            self.reporter.emit("controller.error", status="failed", reason=str(exc))
            raise
        except (WorkflowError, StateError, JiraAdapterError, ProfileError, WorkflowDefinitionError,
                ArtifactLayoutError, AggregationError, ScoringError) as exc:
            self.reporter.emit("controller.error", status="failed", reason=str(exc))
            raise ControllerError(str(exc)) from exc

    def _select(self) -> dict[str, Any]:
        keys = _deduplicate_keys(self.options.issue_keys)
        if self.options.batch_size is not None and self.options.batch_size <= 0:
            raise ControllerError("batch_size must be a positive integer")
        if self.options.batch_offset < 0:
            raise ControllerError("batch_offset must be a non-negative integer")
        client = self.jira or JiraClient()
        evidence: dict[str, Any] = {"selector": "keys", "source": {"args": list(keys)}, "exclusion_reasons": {}}
        if not keys:
            jql = _initial_gate_jql(self.profile)
            candidates = self._discover(client, jql)
            first = next_stage(self.profile, None)
            assert first is not None
            eligible: list[str] = []
            issues: dict[str, dict[str, Any]] = {}
            exclusions: dict[str, list[str]] = {}
            for candidate in candidates:
                key = candidate.get("key") if isinstance(candidate, dict) else None
                if not isinstance(key, str) or not KEY_RE.fullmatch(key):
                    continue
                issue = client.issue(key)
                failures = evaluate_gate(first, _issue_context(issue))
                if failures:
                    exclusions[key] = failures
                    continue
                eligible.append(key)
                issues[key] = issue
            selected = eligible[self.options.batch_offset:]
            if self.options.batch_size is not None:
                selected = selected[:self.options.batch_size]
            evidence = {
                "selector": "profile-gate",
                "source": {"jql": jql, "ordered_candidates": [item["key"] for item in candidates if isinstance(item, dict) and isinstance(item.get("key"), str)]},
                "exclusion_reasons": exclusions,
            }
            return {"keys": selected, "issues": {key: issues[key] for key in selected}, "evidence": evidence}
        selected = keys[self.options.batch_offset:]
        if self.options.batch_size is not None:
            selected = selected[:self.options.batch_size]
        issues = {key: client.issue(key) for key in selected}
        return {"keys": list(selected), "issues": issues, "evidence": evidence}

    def _discover(self, client: JiraClient, jql: str) -> list[dict[str, Any]]:
        start_at = 0
        candidates: list[dict[str, Any]] = []
        while True:
            page = client.search(jql, start_at=start_at, max_results=50)
            issues = page.get("issues", [])
            candidates.extend(issue for issue in issues if isinstance(issue, dict))
            total = page.get("total")
            start_at += len(issues)
            if not issues or (isinstance(total, int) and start_at >= total):
                break
        unique = {item["key"]: item for item in candidates if isinstance(item.get("key"), str)}
        return [unique[key] for key in sorted(unique)]

    def _drive(self, run_id: str) -> dict[str, Any]:
        if self.options.item_parallelism > 1:
            return self._drive_parallel(run_id)
        return self._drive_serial(run_id)

    def _drive_serial(self, run_id: str) -> dict[str, Any]:
        while True:
            envelope = self.core.advance(run_id)
            if envelope["kind"] == "complete":
                result = {"kind": "complete", "status": "complete", "run_id": run_id,
                          "publication": envelope.get("publication"),
                          "duration_seconds": self._elapsed_seconds()}
                publication = result.get("publication")
                if isinstance(publication, dict):
                    for receipt in publication.get("receipts", []):
                        if isinstance(receipt, dict):
                            self.reporter.emit("controller.publication_receipt", run_id=run_id, receipt=receipt)
                usage = _usage_summary(self.core.state.run_dir(run_id) / "events.jsonl")
                result["usage"] = usage
                self.reporter.emit("controller.usage_summary", run_id=run_id, usage=usage)
                self.reporter.emit("controller.run_finished", run_id=run_id, status="complete", result=result,
                                   duration_seconds=result["duration_seconds"])
                return result
            if envelope["kind"] == "blocked":
                result = {"kind": "blocked", "status": "blocked", "run_id": run_id,
                          "reason": envelope["reason"], "duration_seconds": self._elapsed_seconds()}
                self.reporter.emit("controller.run_finished", run_id=run_id, status="blocked", result=result,
                                   duration_seconds=result["duration_seconds"])
                return result
            if envelope["kind"] != "task":
                raise ControllerError(f"unexpected core response: {envelope.get('kind')}")
            self._complete_task(envelope)

    def _drive_parallel(self, run_id: str) -> dict[str, Any]:
        """Bounded item scheduler; reviewers retain their own nested cap."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.options.item_parallelism) as executor:
            in_flight: set[concurrent.futures.Future[None]] = set()
            while True:
                publication_ready = False
                while len(in_flight) < self.options.item_parallelism:
                    envelope = self.core.claim(run_id)
                    kind = envelope["kind"]
                    if kind == "task":
                        in_flight.add(executor.submit(self._complete_task, envelope))
                        continue
                    if kind == "idle":
                        break
                    if kind in {"publication_ready", "complete", "blocked"}:
                        publication_ready = True
                        break
                    raise ControllerError(f"unexpected core claim response: {kind}")
                if in_flight:
                    done, _ = concurrent.futures.wait(
                        in_flight, return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    for future in done:
                        in_flight.remove(future)
                        future.result()
                    continue
                if publication_ready:
                    # Only advance once all item work is submitted. The legacy
                    # core method owns the existing publication barrier/effects.
                    return self._drive_serial(run_id)
                raise ControllerError("parallel scheduler is idle without an active task")

    def _complete_task(self, envelope: dict[str, Any]) -> None:
        run_id = envelope["run_id"]
        self.reporter.emit("controller.task_started", run_id=run_id, task_id=envelope["task_id"],
                           work_id=envelope["work_id"], issue_key=envelope["issue_key"],
                           stage=envelope["stage"], worker=envelope.get("worker"))
        outputs = self._perform_task(envelope)
        if envelope["stage"] == "review":
            self.reporter.emit("controller.review_verdict", **_task_event_context(envelope),
                               review=_review_summary(self.workspace_root, self.profile, envelope["issue_key"]))
        result = {
            "task_id": envelope["task_id"], "run_id": run_id,
            "revision": envelope["expected_revision"], "outputs": outputs,
        }
        self.core.submit(run_id, envelope["task_id"], result)
        self.reporter.emit("controller.task_finished", run_id=run_id, task_id=envelope["task_id"],
                           work_id=envelope["work_id"], issue_key=envelope["issue_key"],
                           stage=envelope["stage"], status="complete")

    def _perform_task(self, task: dict[str, Any]) -> dict[str, str]:
        if task.get("stage") == "review":
            artifact = self._perform_review(task)
        else:
            artifact = self._perform_worker(task)
        outputs: dict[str, str] = {}
        for output_name in task["required_outputs"]:
            if output_name not in artifact:
                raise ControllerError(f"worker did not produce required {output_name}")
            outputs[output_name] = artifact[output_name]
        return outputs

    def _perform_worker(self, task: dict[str, Any]) -> dict[str, str]:
        worker = task.get("worker")
        if not isinstance(worker, str) or not worker.startswith("skill:"):
            raise ControllerError(f"unsupported task worker: {worker}")
        worker_name = worker.removeprefix("skill:")
        definition = self.install_root / "skills" / worker_name / "SKILL.md"
        if not definition.is_file():
            raise ControllerError(f"worker definition does not exist: {definition}")
        paths = self._output_paths(task)
        attempt_dir = self._attempt_dir(task, worker_name, 1)
        prompt = _skill_prompt(
            definition, task, paths, self.install_root, self.workspace_root, self.profile_path,
            self.core.state.request_path(task["run_id"]),
        )
        completion = self._run_with_attempts(
            worker_name, prompt, attempt_dir,
            reset_outputs=lambda: _clear_artifacts(paths.values()),
            event_context=_task_event_context(task),
        )
        _validate_worker_completion(completion, task, paths, worker_name)
        return {name: _read_artifact(path, worker_name) for name, path in paths.items()}

    def _perform_review(self, task: dict[str, Any]) -> dict[str, str]:
        issue_key = task["issue_key"]
        plan = review_plan(self.install_root, self.workspace_root, self.profile, issue_key, self.profile_path)
        assignments = plan["assignments"]
        execution = plan["execution"]
        limit = 1 if execution == "sequential" else min(self.settings["parallelism"], len(assignments))
        failures: list[str] = []
        def run_assignment(assignment: dict[str, Any]) -> None:
            name = assignment["worker"].removeprefix("agent:")
            definition = self.install_root / "agents" / f"{name}.md"
            attempt_dir = self._attempt_dir(task, assignment["reviewer_id"], 1)
            prompt = _agent_prompt(definition, assignment["task"]["prompt"], task, self.install_root, self.workspace_root)
            output_path = Path(assignment["output_path"])
            completion = self._run_with_attempts(
                name, prompt, attempt_dir,
                reset_outputs=lambda: _clear_artifacts((output_path,)),
                event_context={**_task_event_context(task), "reviewer_id": assignment["reviewer_id"]},
            )
            if str(output_path) not in completion:
                raise ControllerError(f"{name} did not acknowledge assigned output path: {output_path}")
            _read_artifact(output_path, name)
        if limit == 1:
            for assignment in assignments:
                try:
                    run_assignment(assignment)
                except ControllerError as exc:
                    failures.append(str(exc))
                    break
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=limit) as executor:
                futures = [executor.submit(run_assignment, assignment) for assignment in assignments]
                for future in futures:
                    try:
                        future.result()
                    except ControllerError as exc:
                        failures.append(str(exc))
        if failures:
            raise ControllerError("reviewer failure: " + "; ".join(failures))
        aggregate = aggregate_review(self.install_root, self.workspace_root, self.profile, issue_key, self.profile_path)
        score_review(self.install_root, self.workspace_root, self.profile, issue_key, self.profile_path)
        return {"review_markdown": _read_artifact(aggregate, "review aggregation")}

    def _run_with_attempts(
        self,
        worker_name: str,
        prompt: str,
        first_attempt_dir: Path,
        *,
        reset_outputs: Callable[[], None],
        event_context: dict[str, Any],
    ) -> str:
        last_error: ControllerError | None = None
        for attempt in range(1, self.settings["max_attempts"] + 1):
            attempt_dir = first_attempt_dir.parent / f"attempt-{attempt}"
            try:
                reset_outputs()
                return self.workers.run(
                    worker_name=worker_name, prompt=prompt, attempt_dir=attempt_dir,
                    event_context={**event_context, "attempt": attempt},
                )
            except ControllerError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    def _attempt_dir(self, task: dict[str, Any], name: str, attempt: int) -> Path:
        return self.core.state.work_dir(task["run_id"], task["work_id"]) / "attempts" / task["task_id"] / name / f"attempt-{attempt}"

    def _elapsed_seconds(self) -> float:
        if self._started_monotonic is None:
            return 0.0
        return round(time.monotonic() - self._started_monotonic, 3)

    def _output_paths(self, task: dict[str, Any]) -> dict[str, Path]:
        layout = ArtifactLayout(self.workspace_root, self.profile)
        stage_definition = stage(self.profile, task["stage"])
        declared = {item["name"]: item["artifact"] for item in stage_definition.get("outputs", [])}
        result: dict[str, Path] = {}
        for output in task["required_outputs"]:
            if output == "strategy_markdown" and uses_feature_document_assembly(stage_definition):
                fragment_path = task.get("fragment_path")
                if not isinstance(fragment_path, str) or not fragment_path:
                    raise ControllerError("assembled refinement task has no fragment_path")
                candidate = Path(fragment_path).resolve()
                if not candidate.is_relative_to(self.core.state.root):
                    raise ControllerError("refinement fragment path must stay in private state")
                result[output] = candidate
                continue
            artifact = declared.get(output)
            if artifact == "task":
                result[output] = layout.task(task["issue_key"])
            elif artifact == "review":
                result[output] = layout.review(task["issue_key"])
            elif isinstance(artifact, str) and artifact.startswith("generated:"):
                result[output] = layout.generated(artifact.removeprefix("generated:"), task["issue_key"])
            else:
                raise ControllerError(f"unsupported artifact target for {output}: {artifact}")
        return result


def _deduplicate_keys(keys: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for key in keys:
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ControllerError(f"invalid issue key: {key!r}")
        if key not in result:
            result.append(key)
    return tuple(result)


def _task_event_context(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": task["run_id"],
        "work_id": task["work_id"],
        "task_id": task["task_id"],
        "issue_key": task["issue_key"],
        "stage": task["stage"],
    }


def _review_summary(workspace_root: Path, profile: dict[str, Any], issue_key: str) -> dict[str, Any]:
    """Read the core-generated score, never a reviewer's prose verdict."""
    score_path = ArtifactLayout(workspace_root, profile).generated("score", issue_key)
    try:
        score = json.loads(score_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"could not read deterministic review score: {score_path}") from exc
    if not isinstance(score, dict):
        raise ControllerError(f"deterministic review score is not an object: {score_path}")
    fields = ("verdict", "total", "maximum", "zero_count", "needs_attention", "label")
    return {field: score.get(field) for field in fields if field in score}


def _usage_summary(event_path: Path) -> dict[str, Any]:
    """Aggregate terminal Claude usage records from the private JSONL trace."""
    totals = {
        "workers": 0,
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "thinking_tokens": 0,
        "models": {},
    }
    try:
        lines = event_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return totals
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or record.get("type") != "claude.stream":
            continue
        event = record.get("event")
        if not isinstance(event, dict) or event.get("type") != "result" or event.get("is_error"):
            continue
        model_usage = event.get("modelUsage")
        if not isinstance(model_usage, dict):
            continue
        totals["workers"] += 1
        for model, values in model_usage.items():
            if not isinstance(model, str) or not isinstance(values, dict):
                continue
            target = totals["models"].setdefault(model, {
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "thinking_tokens": 0,
            })
            for source, destination in (
                ("costUSD", "cost_usd"),
                ("inputTokens", "input_tokens"),
                ("outputTokens", "output_tokens"),
                ("cacheReadInputTokens", "cache_read_input_tokens"),
                ("cacheCreationInputTokens", "cache_creation_input_tokens"),
                ("thinkingTokens", "thinking_tokens"),
            ):
                value = values.get(source, 0)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    target[destination] += value
                    totals[destination] += value
    totals["cost_usd"] = round(totals["cost_usd"], 8)
    for values in totals["models"].values():
        values["cost_usd"] = round(values["cost_usd"], 8)
    return totals


def _read_artifact(path: Path, worker_name: str) -> str:
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ControllerError(f"{worker_name} did not write {path}") from exc
    if not content.strip():
        raise ControllerError(f"{worker_name} wrote an empty artifact: {path}")
    return content


def _validate_worker_completion(
    completion: str,
    task: dict[str, Any],
    output_paths: dict[str, Path],
    worker_name: str,
) -> None:
    """Require a bounded worker to identify the exact task artifact it wrote."""
    record = _json_object_in(completion)
    if record is None or record.get("status") not in {"written", "complete"}:
        raise ControllerError(f"{worker_name} did not return a valid completion record")
    if record.get("issue_key") != task["issue_key"]:
        raise ControllerError(f"{worker_name} completion issue_key does not match the task")
    artifact_path = record.get("artifact_path")
    if artifact_path not in {str(path) for path in output_paths.values()}:
        raise ControllerError(f"{worker_name} completion artifact_path is not task-owned")


def _json_object_in(value: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate
    return None


def _clear_artifacts(paths: Sequence[Path]) -> None:
    """Remove only task-owned outputs before an attempt can create them."""
    for path in paths:
        if path.exists() and not path.is_file():
            raise ControllerError(f"task output path is not a file: {path}")
        path.unlink(missing_ok=True)


def _skill_prompt(definition: Path, task: dict[str, Any], output_paths: dict[str, Path],
                  install_root: Path, workspace_root: Path, profile_path: str,
                  source_request_path: Path) -> str:
    return (
        "You are a bounded ADLC worker launched by a deterministic controller.\n"
        f"Read and follow the checked-in worker instructions at {definition}.\n"
        "The task and paths below are authoritative. Do not invoke adlc-workflow, adlc-task, "
        "adlc-submit, another Claude process, or Jira. Source Jira data and context were already prepared.\n"
        f"Install root: {install_root}\nWorkspace: {workspace_root}\nProfile: {profile_path}\n"
        f"Task: {json.dumps(task, sort_keys=True)}\n"
        f"Captured source request: {source_request_path}\n"
        f"Worker resources: {json.dumps(task.get('worker_resources', {}), sort_keys=True)}\n"
        f"Required output paths: {json.dumps({key: str(value) for key, value in output_paths.items()}, sort_keys=True)}\n"
        "Write each required artifact to its assigned path. Return only a compact JSON completion record."
    )


def _agent_prompt(definition: Path, assignment_prompt: str, task: dict[str, Any],
                  install_root: Path, workspace_root: Path) -> str:
    return (
        "You are one independent ADLC reviewer launched by a deterministic controller.\n"
        f"Read and follow the checked-in reviewer definition at {definition}.\n"
        "Do not invoke workflow commands, Jira, or other agents. Write only the assigned review artifact.\n"
        f"Install root: {install_root}\nWorkspace: {workspace_root}\n"
        f"Task: {json.dumps(task, sort_keys=True)}\n"
        "Assignment:\n" + assignment_prompt + "\n"
        "Return only the assigned output path and a short verdict."
    )
