"""Freeze worker inputs so every candidate sees identical bytes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .config import Experiment
from .records import EvalError, read_json, relative, sha256_file, utc_now, write_json


EVAL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = EVAL_ROOT.parent
PLUGIN_ALLOWLIST = (
    ".claude-plugin", "skills", "agents", "scripts", "src", "config", "templates",
    "schemas", "context", "policies", "plugins", "adapters", "CLAUDE.md", "pyproject.toml",
)
PLUGIN_EXCLUDES = {"evals", "workspace", "artifacts", ".adlc", ".git", ".env", ".venv", "tests", "docs"}


def _safe_copy(source: Path, destination: Path, source_root: Path) -> None:
    source_root = source_root.resolve()
    if source.is_file():
        try:
            resolved = source.resolve()
            resolved.relative_to(source_root)
        except ValueError as exc:
            raise EvalError(f"snapshot source escapes root: {source}") from exc
        target = destination / source.name if destination.is_dir() or not destination.suffix else destination
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, target)
        target.chmod(stat.S_IMODE(resolved.stat().st_mode))
        return
    for path in sorted(source.rglob("*")):
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        try:
            resolved = path.resolve()
            resolved.relative_to(source_root)
        except ValueError as exc:
            raise EvalError(f"snapshot source symlink escapes root: {path}") from exc
        target = destination / path.relative_to(source)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(resolved, target)
            target.chmod(stat.S_IMODE(resolved.stat().st_mode))


def _manifest(root: Path) -> dict[str, Any]:
    files = []
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        mode = stat.S_IMODE(path.stat().st_mode)
        files.append({"path": path.relative_to(root).as_posix(), "bytes": path.stat().st_size,
                      "sha256": sha256_file(path), "mode": mode})
    digest = hashlib.sha256(json.dumps(files, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    return {"files": files, "sha256": digest}


def _copy_plugin(destination: Path) -> dict[str, Any]:
    for name in PLUGIN_ALLOWLIST:
        source = PROJECT_ROOT / name
        if not source.exists():
            continue
        if source.is_dir():
            _safe_copy(source, destination / name, PROJECT_ROOT)
        else:
            _safe_copy(source, destination, PROJECT_ROOT)
    for excluded in PLUGIN_EXCLUDES:
        if (destination / excluded).exists():
            raise EvalError(f"runtime snapshot contains excluded path: {excluded}")
    required = ("src/adlc_workflow/controller.py", "skills/rhai-feature-refine-worker/SKILL.md",
                "config/rhai-feature-creator.yaml", ".claude-plugin/marketplace.json")
    missing = [item for item in required if not (destination / item).is_file()]
    if missing:
        raise EvalError(f"runtime snapshot missing required resources: {', '.join(missing)}")
    return _manifest(destination)


def _copy_eval_runtime(destination: Path) -> dict[str, Any]:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("scripts", "eval_lib"):
        _safe_copy(EVAL_ROOT / name, destination / name, EVAL_ROOT)
    return _manifest(destination)


def _context_source(experiment: Experiment, staging: Path) -> Path:
    if experiment.context_workspace:
        context = experiment.context_workspace / ".context"
        if not context.is_dir():
            raise EvalError(f"prepared workspace has no .context directory: {experiment.context_workspace}")
        return context
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    from adlc_workflow.context import prepare_context
    from adlc_workflow.profiles import load_profile, resolve_profile_path
    profile_path = resolve_profile_path(PROJECT_ROOT, experiment.profile)
    profile = load_profile(PROJECT_ROOT, profile_path)
    prepare_context(PROJECT_ROOT, staging, profile, "eval")
    return staging / ".context"


def _validate_context(context: Path, plugin_root: Path, experiment: Experiment) -> None:
    manifest_path = context / "context-manifest.json"
    manifest = read_json(manifest_path)
    if manifest.get("status") != "prepared" or not isinstance(manifest.get("sources"), list) or not manifest["sources"]:
        raise EvalError("prepared context manifest must declare non-empty prepared sources")
    sys.path.insert(0, str(plugin_root / "src"))
    from adlc_workflow.profiles import load_profile, resolve_profile_path
    profile_path = resolve_profile_path(plugin_root, experiment.profile)
    profile = load_profile(plugin_root, profile_path)
    expected = {source["destination"] for source in profile.get("context_sources", []) if isinstance(source, dict)}
    actual = {item.get("destination") for item in manifest["sources"] if isinstance(item, dict)}
    if not expected.issubset(actual):
        raise EvalError(f"prepared context does not satisfy profile destinations: expected {sorted(expected)}, got {sorted(actual)}")
    for source in manifest["sources"]:
        if not isinstance(source, dict) or not isinstance(source.get("destination"), str):
            raise EvalError("prepared context source is malformed")
        root = context.parent / source["destination"]
        if not root.is_dir() or not (root / str(source.get("version_file", "LATEST_VERSION"))).is_file():
            raise EvalError(f"prepared context source is incomplete: {source.get('destination')}")
        usage = source.get("usage")
        if usage and not (plugin_root / str(usage)).is_file():
            raise EvalError(f"prepared context usage document is unavailable: {usage}")


def _git_metadata(root: Path) -> dict[str, Any]:
    def run(*args: str) -> str | None:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    return {"head": run("rev-parse", "HEAD"), "dirty": bool(run("status", "--porcelain"))}


def _image_id(image: str) -> str | None:
    result = subprocess.run(["podman", "image", "inspect", "--format", "{{.Id}}", image], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def schedule(experiment: Experiment) -> list[dict[str, Any]]:
    values = []
    index = 0
    for case in sorted(experiment.cases, key=lambda item: item.id):
        for repetition in range(1, experiment.repetitions + 1):
            pair = ("A", "B") if index % 2 == 0 else ("B", "A")
            for candidate in pair:
                values.append({"case_id": case.id, "repetition": repetition, "candidate": candidate, "order": len(values) + 1})
            index += 1
    return values


def prepare(experiment: Experiment, result_root: Path, experiment_id: str) -> Path:
    root = (result_root / experiment_id).resolve()
    if root.exists():
        raise EvalError(f"experiment already exists: {root}")
    root.mkdir(parents=True)
    resources = root / "resources"
    plugin = resources / "plugin"
    runtime = resources / "eval-runtime"
    context_destination = resources / "context" / ".context"
    try:
        plugin_manifest = _copy_plugin(plugin)
        runtime_manifest = _copy_eval_runtime(runtime)
        staging = root / ".prepare-context"
        context_source = _context_source(experiment, staging)
        _safe_copy(context_source, context_destination, context_source)
        _validate_context(context_destination, plugin, experiment)
        context_manifest = _manifest(context_destination)
        for case in experiment.cases:
            _safe_copy(case.path, resources / "cases" / case.id, case.path)
            for context_file in case.judge_context_files:
                requested = (context_destination / context_file).resolve()
                try:
                    requested.relative_to(context_destination.resolve())
                except ValueError as exc:
                    raise EvalError(f"judge context path escapes context root: {context_file}") from exc
                if not requested.is_file():
                    raise EvalError(f"judge context file does not exist: {context_file}")
        shutil.copyfile(experiment.suite.path, resources / "suite.yaml")
        shutil.copyfile(experiment.judge_prompt, resources / "judge-prompt.md")
        frozen_config = yaml.safe_load(experiment.path.read_text(encoding="utf-8"))
        frozen_config["suite"] = "suite.yaml"
        frozen_config["cases"] = [f"cases/{case.id}" for case in experiment.cases]
        frozen_config["context"] = {"prepared_workspace": "context"}
        frozen_config["judge"]["prompt"] = "judge-prompt.md"
        (resources / "experiment.yaml").write_text(yaml.safe_dump(frozen_config, sort_keys=False), encoding="utf-8")
        file_manifest = {"plugin": plugin_manifest, "eval_runtime": runtime_manifest, "context": context_manifest,
                         "cases": _manifest(resources / "cases"), "suite": sha256_file(resources / "suite.yaml"),
                         "judge_prompt": sha256_file(resources / "judge-prompt.md"),
                         "experiment": sha256_file(resources / "experiment.yaml")}
        write_json(resources / "file-manifest.json", file_manifest)
        planned = schedule(experiment)
        write_json(root / "schedule.json", {"schema_version": 1, "order": experiment.order, "trials": planned})
        effective = yaml.safe_load(experiment.path.read_text(encoding="utf-8"))
        write_json(root / "experiment.json", {
            "schema_version": 1, "experiment_id": experiment_id, "name": experiment.name,
            "prepared_at": utc_now(), "effective_config": effective, "source_config": str(experiment.path),
            "plugin_git": _git_metadata(PROJECT_ROOT), "plugin_snapshot": file_manifest["plugin"]["sha256"],
            "eval_runtime_snapshot": file_manifest["eval_runtime"]["sha256"], "context_snapshot": file_manifest["context"]["sha256"],
            "image": {"requested": experiment.container_image, "id": _image_id(experiment.container_image)},
            "python": sys.version, "resource_manifest": "resources/file-manifest.json",
        })
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise
    return root
