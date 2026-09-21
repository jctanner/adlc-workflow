"""Reviewed lifecycle-profile and artifact-layout loading."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml


class ProfileError(ValueError):
    """Raised when a profile is missing or invalid."""


def resolve_profile_path(project_root: str | Path, profile: str | None) -> str:
    """Resolve a profile name or install-relative path safely.

    A name is the configuration filename stem (for example,
    ``rhai-feature-creator``), rather than the shared package ``id`` found in
    every profile document.
    """
    root = Path(project_root).absolute()
    value = profile or "rhai-feature-creator"
    if not isinstance(value, str) or not value.strip():
        raise ProfileError("profile must be a non-empty name or path")
    candidate = Path(value.strip())
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProfileError("profile path must stay within project root")
    if candidate.suffix:
        relative = candidate
    elif len(candidate.parts) == 1:
        relative = Path("config") / f"{candidate.name}.yaml"
    else:
        relative = candidate.with_suffix(".yaml")
    path = root / relative
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ProfileError("profile path must stay within project root") from exc
    if not path.is_file():
        raise ProfileError(f"profile does not exist: {relative}")
    return str(relative)


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_LIFECYCLE_RE = re.compile(r"^adlc-[a-z0-9-]+-v[0-9]+$")


def _relative_path(value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ProfileError(f"profile requires a non-empty {field}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ProfileError(f"profile path must stay within its root: {field}")


def _resource(root: Path, value: Any, field: str) -> None:
    if not isinstance(value, str) or not value:
        raise ProfileError(f"profile requires a non-empty {field}")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ProfileError(f"profile resource must stay within install root: {field}")
    if not (root / path).is_file():
        raise ProfileError(f"profile resource does not exist: {field}: {value}")


def validate_profile(project_root: str | Path, value: dict[str, Any]) -> None:
    """Validate the install-time contract shared by all lifecycle profiles."""
    root = Path(project_root).absolute()
    if value.get("api_version") != "adlc.profile/v1":
        raise ProfileError("profile api_version must be adlc.profile/v1")
    if not isinstance(value.get("id"), str) or not _NAME_RE.fullmatch(value["id"]):
        raise ProfileError("profile id must be a lowercase kebab-case name")
    if not isinstance(value.get("lifecycle"), str) or not _LIFECYCLE_RE.fullmatch(value["lifecycle"]):
        raise ProfileError("profile lifecycle must match adlc-<name>-vN")

    controller = value.get("controller", {})
    if controller is not None:
        if not isinstance(controller, dict):
            raise ProfileError("profile controller must be a mapping")
        for field in ("reviewer_parallelism", "max_attempts"):
            candidate = controller.get(field)
            if candidate is not None and (
                not isinstance(candidate, int) or isinstance(candidate, bool) or candidate <= 0
            ):
                raise ProfileError(f"controller.{field} must be a positive integer")
        timeout = controller.get("task_timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0
        ):
            raise ProfileError("controller.task_timeout_seconds must be positive")
        model = controller.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            raise ProfileError("controller.model must be a non-empty string")

    sources = value.get("context_sources", [])
    if not isinstance(sources, list):
        raise ProfileError("profile context_sources must be a list")
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            raise ProfileError(f"context_sources[{index}] must be a mapping")
        for key in ("id", "repo", "destination"):
            if not isinstance(source.get(key), str) or not source[key]:
                raise ProfileError(f"context_sources[{index}] requires {key}")
        _relative_path(source["destination"], f"context_sources[{index}].destination")
        if source.get("usage") is not None:
            _resource(root, source["usage"], f"context_sources[{index}].usage")
        if source.get("version_file") is not None:
            _relative_path(source["version_file"], f"context_sources[{index}].version_file")
        for key in ("include", "exclude"):
            patterns = source.get(key, [])
            if not isinstance(patterns, list) or not all(isinstance(pattern, str) for pattern in patterns):
                raise ProfileError(f"context_sources[{index}].{key} must be a list of strings")

    workflow = value.get("workflow")
    if not isinstance(workflow, dict) or not isinstance(workflow.get("stages"), list) or not workflow["stages"]:
        raise ProfileError("profile workflow requires a non-empty stages list")
    stages = workflow["stages"]
    stage_ids: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict) or not isinstance(stage.get("id"), str) or not _NAME_RE.fullmatch(stage["id"]):
            raise ProfileError("each workflow stage requires a lowercase kebab-case id")
        if stage["id"] in stage_ids:
            raise ProfileError(f"duplicate workflow stage: {stage['id']}")
        stage_ids.add(stage["id"])
        if stage.get("kind") != "publication":
            worker = stage.get("worker")
            if not isinstance(worker, str) or not worker.startswith("skill:"):
                raise ProfileError(f"stage {stage['id']} requires a skill worker reference")
            skill_name = worker.removeprefix("skill:")
            if not _NAME_RE.fullmatch(skill_name) or not (root / "skills" / skill_name / "SKILL.md").is_file():
                raise ProfileError(f"stage {stage['id']} references a missing skill worker: {worker}")
        assembly = stage.get("assembly")
        if assembly is not None:
            if stage.get("id") != "refine" or not isinstance(assembly, dict) or assembly.get("renderer") != "core:rhai-feature-document-v1":
                raise ProfileError(f"stage {stage['id']} has an unsupported assembly declaration")
        outputs = stage.get("outputs", [])
        if not isinstance(outputs, list):
            raise ProfileError(f"stage {stage['id']} outputs must be a list")
        for output in outputs:
            if not isinstance(output, dict) or not isinstance(output.get("name"), str):
                raise ProfileError(f"stage {stage['id']} outputs require named mappings")
            artifact = output.get("artifact")
            if not isinstance(artifact, str) or artifact not in {"task", "review"} and not artifact.startswith("generated:"):
                raise ProfileError(f"stage {stage['id']} has an invalid artifact target")

    publication = [stage for stage in stages if stage.get("kind") == "publication"]
    if len(publication) != 1:
        raise ProfileError("profile workflow requires exactly one publication stage")
    adapters = publication[0].get("adapters")
    if not isinstance(adapters, dict):
        raise ProfileError("publication stage requires adapters by mode")
    for mode in ("production", "local", "eval"):
        if mode not in adapters or not isinstance(adapters[mode], list) or not all(isinstance(item, str) for item in adapters[mode]):
            raise ProfileError(f"publication adapters must define a list for {mode}")

    refine = next((stage for stage in stages if stage["id"] == "refine"), None)
    if refine is not None:
        template = refine.get("template")
        if not isinstance(template, dict):
            raise ProfileError("refine stage requires a template mapping")
        _resource(root, template.get("path"), "workflow.stages.refine.template.path")

    review = next((stage for stage in stages if stage["id"] == "review"), None)
    if review is not None:
        reviewers = review.get("reviewers")
        if not isinstance(reviewers, list) or not reviewers:
            raise ProfileError("review stage requires reviewers")
        reviewer_ids: set[str] = set()
        for reviewer in reviewers:
            if not isinstance(reviewer, dict) or not isinstance(reviewer.get("id"), str) or not _NAME_RE.fullmatch(reviewer["id"]):
                raise ProfileError("reviewers require lowercase kebab-case ids")
            if reviewer["id"] in reviewer_ids:
                raise ProfileError(f"duplicate reviewer: {reviewer['id']}")
            reviewer_ids.add(reviewer["id"])
            worker = reviewer.get("worker")
            if not isinstance(worker, str) or not re.fullmatch(r"agent:[a-z0-9-]+", worker):
                raise ProfileError(f"reviewer must reference an agent: {worker}")
            agent_name = worker.removeprefix("agent:")
            if not (root / "agents" / f"{agent_name}.md").is_file():
                raise ProfileError(f"missing reviewer agent: {agent_name}")
            artifact = reviewer.get("result", {}).get("artifact") if isinstance(reviewer.get("result"), dict) else None
            if not isinstance(artifact, dict) or not isinstance(artifact.get("root"), str) or not isinstance(artifact.get("filename"), str):
                raise ProfileError(f"reviewer {reviewer['id']} requires a result artifact")
        aggregate = review.get("aggregate")
        if not isinstance(aggregate, dict):
            raise ProfileError("review requires aggregate configuration")
        aggregate_inputs = aggregate.get("inputs")
        if not isinstance(aggregate_inputs, list) or not aggregate_inputs or not set(aggregate_inputs).issubset(reviewer_ids):
            raise ProfileError("review.aggregate.inputs must reference configured reviewers")
        _resource(root, aggregate.get("schema"), "workflow.stages.review.aggregate.schema")
        if not isinstance(aggregate.get("renderer"), str) or not aggregate["renderer"]:
            raise ProfileError("review.aggregate.renderer is required")
        scoring = review.get("scoring")
        if not isinstance(scoring, dict) or not isinstance(scoring.get("inputs"), list) or not set(scoring["inputs"]).issubset(reviewer_ids):
            raise ProfileError("review.scoring.inputs must reference configured reviewers")
        rubric = scoring.get("rubric")
        if not isinstance(rubric, dict):
            raise ProfileError("review.scoring.rubric is required")
        _resource(root, rubric.get("path"), "workflow.stages.review.scoring.rubric.path")
        if not isinstance(scoring.get("verdict"), str) or not scoring["verdict"].startswith("core:"):
            raise ProfileError("review.scoring.verdict must name a core engine")

    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ProfileError("profile requires an artifacts mapping")
    for field in ("bundle_root", "result_file"):
        _relative_path(artifacts.get(field), f"artifacts.{field}")
    roots = artifacts.get("roots")
    if not isinstance(roots, dict) or not roots:
        raise ProfileError("artifacts.roots must be a non-empty mapping")
    for name, path in roots.items():
        _relative_path(path, f"artifacts.roots.{name}")
    evidence = artifacts.get("evidence_files", {})
    if not isinstance(evidence, dict):
        raise ProfileError("artifacts.evidence_files must be a mapping")
    for name, path in evidence.items():
        _relative_path(path, f"artifacts.evidence_files.{name}")
    generated = artifacts.get("generated", {})
    if not isinstance(generated, dict):
        raise ProfileError("artifacts.generated must be a mapping")
    for name, declaration in generated.items():
        if not isinstance(declaration, dict):
            raise ProfileError(f"artifacts.generated.{name} must be a mapping")
        if declaration.get("root") not in roots or not isinstance(declaration.get("filename"), str):
            raise ProfileError(f"artifacts.generated.{name} requires a declared root and filename")
    private_state = value.get("private_state")
    if not isinstance(private_state, dict):
        raise ProfileError("profile requires private_state")
    _relative_path(private_state.get("root"), "private_state.root")



def load_profile(project_root: str | Path, profile_path: str) -> dict[str, Any]:
    # Do not resolve symlinks before the containment check. The Claude local
    # marketplace intentionally links cached plugin files back to the mounted
    # install, and those links are still part of the selected install root.
    root = Path(project_root).absolute()
    resolved_profile = resolve_profile_path(root, profile_path)
    path = root / resolved_profile
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"cannot load profile {resolved_profile}: {exc}") from exc
    if not isinstance(value, dict):
        raise ProfileError("profile must contain a mapping")
    for key in ("api_version", "id", "lifecycle"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ProfileError(f"profile requires non-empty {key}")
    validate_profile(root, value)
    return value


def resource_path(project_root: str | Path, profile: dict[str, Any], resource: str, field: str) -> Path:
    """Resolve a profile-owned install resource without allowing path escape."""
    root = Path(project_root).absolute()
    if not isinstance(resource, str) or not resource:
        raise ProfileError(f"profile requires a non-empty {field}")
    path = Path(resource)
    if path.is_absolute() or ".." in path.parts:
        raise ProfileError(f"profile resource must stay within project root: {field}")
    resolved = root / path
    if not resolved.is_file():
        raise ProfileError(f"profile resource does not exist: {field}: {resource}")
    return resolved


def refine_template_path(project_root: str | Path, profile: dict[str, Any]) -> Path:
    """Return the installed template declared by the refine workflow stage."""
    workflow = profile.get("workflow")
    stages = workflow.get("stages") if isinstance(workflow, dict) else None
    if not isinstance(stages, list):
        raise ProfileError("profile workflow requires stages to resolve refine.template.path")
    refine = next((item for item in stages if isinstance(item, dict) and item.get("id") == "refine"), None)
    if refine is None:
        raise ProfileError("profile has no refine stage")
    template = refine.get("template")
    if not isinstance(template, dict):
        raise ProfileError("refine stage requires a template mapping")
    return resource_path(project_root, profile, template.get("path"), "workflow.stages.refine.template.path")
