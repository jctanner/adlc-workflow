"""Host-side fresh-container candidate collection."""

from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import Experiment, load_experiment
from .metrics import usage
from .production_bridge import build_task
from .records import EvalError, read_json, relative, sha256_file, utc_now, write_json


def load_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if "=" not in text:
            raise EvalError(f"invalid .env line: {line}")
        key, value = text.split("=", 1)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise EvalError(f"invalid .env key: {key}")
        values[key] = value
    return values


def _experiment(experiment_root: Path) -> Experiment:
    config = experiment_root / "resources" / "experiment.yaml"
    if not config.is_file():
        raise EvalError(f"experiment has no frozen configuration: {config}")
    return load_experiment(config)


def _trial_root(root: Path, case_id: str, repetition: int, candidate: str) -> Path:
    return root / "trials" / case_id / f"repetition-{repetition:02d}" / candidate


def _case(experiment: Experiment, identifier: str):
    return next(item for item in experiment.cases if item.id == identifier)


def _stage_trial(root: Path, experiment: Experiment, scheduled: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    trial = _trial_root(root, scheduled["case_id"], int(scheduled["repetition"]), scheduled["candidate"])
    if (trial / "execution.json").is_file():
        return trial, read_json(trial / "execution.json")
    trial.mkdir(parents=True, exist_ok=False)
    (trial / "workspace" / ".adlc-eval").mkdir(parents=True)
    input_dir = trial / "input"
    input_dir.mkdir()
    case = _case(experiment, scheduled["case_id"])
    source = json.loads(case.source_issue.read_text(encoding="utf-8"))
    if not isinstance(source, dict) or source.get("key") != case.issue_key:
        raise EvalError(f"case {case.id} source issue must be an object with key {case.issue_key}")
    plugin = root / "resources" / "plugin"
    task, profile_path, _ = build_task(plugin, Path("/workspace"), experiment.profile, case.issue_key)
    task["profile_path"] = profile_path
    request = {"args": [case.issue_key], "kwargs": {"profile": profile_path, "source_issues": {case.issue_key: source}}}
    write_json(input_dir / "task.json", task)
    write_json(input_dir / "request.json", request)
    manifest = read_json(root / "resources" / "file-manifest.json")
    signature_material = json.dumps({"task": sha256_file(input_dir / "task.json"), "request": sha256_file(input_dir / "request.json"),
                                     "plugin": manifest["plugin"]["sha256"], "context": manifest["context"]["sha256"],
                                     "runtime": manifest["eval_runtime"]["sha256"], "invocation": experiment.invocation}, sort_keys=True).encode()
    (trial / "launch.json").write_text(json.dumps({"schema_version": 1, "case_id": case.id,
        "repetition": scheduled["repetition"], "candidate": scheduled["candidate"],
        "model_requested": experiment.candidates[scheduled["candidate"]].model,
        "worker": experiment.worker, "profile": profile_path, "invocation": experiment.invocation,
        "input_signature": hashlib.sha256(signature_material).hexdigest()}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return trial, {}


def _compose_env(root: Path, trial: Path, experiment: Experiment, candidate: str, dotenv: dict[str, str]) -> dict[str, str]:
    merged = os.environ.copy()
    merged.update(dotenv)
    adc = merged.get("ADLC_HOST_ADC_PATH")
    if not adc or not Path(adc).is_file():
        raise EvalError("ADLC_HOST_ADC_PATH must name a readable credentials file")
    project = merged.get("ANTHROPIC_VERTEX_PROJECT_ID")
    if not project:
        raise EvalError("ANTHROPIC_VERTEX_PROJECT_ID is required for live collection")
    image = read_json(root / "experiment.json").get("image", {}).get("id") or experiment.container_image
    dummy = root / ".compose-unused"
    dummy.mkdir(exist_ok=True)
    merged.update({
        "ADLC_EVAL_IMAGE": str(image), "ADLC_EVAL_PLUGIN_DIR": str(root / "resources" / "plugin"),
        "ADLC_EVAL_RUNTIME_DIR": str(root / "resources" / "eval-runtime"),
        "ADLC_EVAL_WORKSPACE_DIR": str(trial / "workspace"), "ADLC_EVAL_INPUT_DIR": str(trial / "input"),
        "ADLC_EVAL_CONTEXT_DIR": str(root / "resources" / "context" / ".context"),
        "ADLC_EVAL_JUDGE_INPUT_DIR": str(dummy), "ADLC_EVAL_JUDGE_OUTPUT_DIR": str(dummy),
        "ADLC_EVAL_MODEL": experiment.candidates[candidate].model,
        "ADLC_EVAL_TIMEOUT_SECONDS": str(experiment.worker_timeout_seconds),
    })
    return merged


def _project_name(root: Path) -> str:
    text = re.sub(r"[^a-z0-9]", "-", root.name.lower()).strip("-")[:35]
    return f"adlc-eval-{text or 'run'}"


def collect(root: Path, candidate_filter: str | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
    root = root.resolve()
    experiment = _experiment(root)
    schedule = read_json(root / "schedule.json").get("trials", [])
    if not isinstance(schedule, list):
        raise EvalError("invalid frozen schedule")
    selected = [item for item in schedule if candidate_filter is None or item.get("candidate") == candidate_filter]
    if candidate_filter and candidate_filter not in {"A", "B"}:
        raise EvalError("candidate must be A or B")
    dotenv = load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    output: list[dict[str, Any]] = []
    lock = root / ".collection.lock"
    if not dry_run:
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(descriptor)
        except FileExistsError as exc:
            raise EvalError(f"collection is already active for {root}") from exc
    try:
        for scheduled in selected:
            if dry_run:
                trial = _trial_root(root, str(scheduled["case_id"]), int(scheduled["repetition"]), str(scheduled["candidate"]))
                command = ["podman-compose", "-f", str(Path(__file__).resolve().parents[1] / "podman-compose.yaml"),
                           "--project-name", _project_name(root), "run", "--rm", "--no-deps", "worker"]
                output.append({"trial": relative(trial, root), "status": "planned",
                               "model": experiment.candidates[str(scheduled["candidate"])].model,
                               "worker": experiment.worker, "command": command, "workspace": str(trial / "workspace")})
                continue
            trial, existing = _stage_trial(root, experiment, scheduled)
            if existing:
                output.append({"trial": relative(trial, root), "status": "skipped", "reason": "terminal record exists"})
                continue
            env = _compose_env(root, trial, experiment, str(scheduled["candidate"]), dotenv)
            command = ["podman-compose", "-f", str(Path(__file__).resolve().parents[1] / "podman-compose.yaml"),
                       "--project-name", _project_name(root), "run", "--rm", "--no-deps", "worker"]
            start = time.monotonic()
            started_at = utc_now()
            timed_out = False
            try:
                process = subprocess.run(command, cwd=root, env=env, text=True, capture_output=True,
                                         timeout=experiment.worker_timeout_seconds + experiment.startup_timeout_seconds + 45)
                returncode = process.returncode
                stdout, stderr = process.stdout, process.stderr
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                returncode = -1
                stdout = exc.stdout or ""
                stderr = (exc.stderr or "") + "\ncontainer collection timed out"
            duration = time.monotonic() - start
            (trial / "container.stdout.log").write_text(stdout, encoding="utf-8", errors="replace")
            (trial / "container.stderr.log").write_text(stderr, encoding="utf-8", errors="replace")
            worker_record_path = trial / "workspace" / ".adlc-eval" / "container-result.json"
            worker_record = read_json(worker_record_path) if worker_record_path.is_file() else {}
            events = trial / "workspace" / ".adlc-eval" / "events.jsonl"
            launch = read_json(trial / "launch.json")
            observed = sorted(usage(events).get("models", {}))
            accepted = list(experiment.candidates[scheduled["candidate"]].accepted_resolved_models)
            model_ok = bool(observed) and set(observed).issubset(set(accepted))
            status = "timed_out" if timed_out else worker_record.get("status", "failed")
            record = {"schema_version": 1, **launch, "status": status, "started_at": started_at, "finished_at": utc_now(),
                      "container_duration_seconds": round(duration, 4), "container_returncode": returncode,
                      "worker_result": relative(worker_record_path, root) if worker_record_path.exists() else None,
                      "events": relative(events, root) if events.exists() else None,
                      "usage": usage(events), "models_observed": observed, "model_match": model_ok,
                      "accepted_resolved_models": accepted,
                      "artifact": relative(trial / "workspace" / "artifacts", root) if (trial / "workspace" / "artifacts").exists() else None,
                      "logs": {"stdout": relative(trial / "container.stdout.log", root), "stderr": relative(trial / "container.stderr.log", root)},
                      "comparison_eligible": status == "succeeded" and model_ok,
                      "ineligibility_reasons": ([] if status == "succeeded" else [f"worker status: {status}"]) + ([] if model_ok else ["missing or unexpected observed model"])}
            write_json(trial / "execution.json", record)
            output.append({"trial": relative(trial, root), "status": status, "model_match": model_ok})
    finally:
        if not dry_run:
            lock.unlink(missing_ok=True)
    return output
