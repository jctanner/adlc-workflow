"""Profile-selected public artifact layout helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


class ArtifactLayoutError(ValueError):
    """Raised when a profile contains an unsafe or incomplete artifact layout."""


class ArtifactLayout:
    """Resolve public artifact paths from a workflow profile.

    Profile paths are workspace-relative. Keeping this resolution in one place
    prevents the core and publication adapters from silently assuming the
    feature workflow's directory names.
    """

    def __init__(self, workspace_root: Path, profile: dict[str, Any]):
        self.profile = profile
        self.workspace_root = workspace_root.resolve()
        config = profile.get("artifacts")
        if not isinstance(config, dict):
            raise ArtifactLayoutError("profile requires an artifacts mapping")
        self.bundle_root = self._path(config.get("bundle_root", "artifacts"), "bundle_root")
        self.result_file = self._path(config.get("result_file"), "result_file", base=self.bundle_root)
        roots = config.get("roots")
        if not isinstance(roots, dict):
            raise ArtifactLayoutError("profile artifacts requires a roots mapping")
        self.roots = {
            str(name): self._path(value, f"roots.{name}")
            for name, value in roots.items()
        }
        evidence = config.get("evidence_files", {})
        if not isinstance(evidence, dict):
            raise ArtifactLayoutError("profile artifacts evidence_files must be a mapping")
        self.evidence = {str(name): str(value) for name, value in evidence.items()}
        self._validate_reserved_names(config)

    def _validate_reserved_names(self, config: dict[str, Any]) -> None:
        """Reject generated artifact names that can shadow bundle metadata."""
        reserved = {"manifest.json", "context-manifest.json"}
        result_file = config.get("result_file")
        if isinstance(result_file, str) and result_file:
            reserved.add(Path(result_file).name)
        reserved.update(
            Path(value).name
            for value in self.evidence.values()
            if isinstance(value, str) and value
        )

        declarations: list[tuple[str, Any, dict[str, str]]] = []
        workflow = self.profile.get("workflow")
        stage_definitions = workflow.get("stages") if isinstance(workflow, dict) else []
        if isinstance(stage_definitions, list):
            for stage_definition in stage_definitions:
                if not isinstance(stage_definition, dict):
                    continue
                aggregate = stage_definition.get("aggregate")
                if isinstance(aggregate, dict) and "artifact" in aggregate:
                    declarations.append(("aggregate artifact", aggregate["artifact"], {"issue_key": "RHAIRFE-1"}))
                reviewers = stage_definition.get("reviewers", [])
                if isinstance(reviewers, list):
                    for reviewer in reviewers:
                        if not isinstance(reviewer, dict):
                            continue
                        result = reviewer.get("result")
                        if isinstance(result, dict) and "artifact" in result:
                            declarations.append((
                                f"reviewer {reviewer.get('id', '<unknown>')} artifact",
                                result["artifact"],
                                {"issue_key": "RHAIRFE-1", "reviewer_id": "reviewer"},
                            ))

        for label, artifact, values in declarations:
            if not isinstance(artifact, dict) or not isinstance(artifact.get("filename"), str):
                continue
            try:
                filename = artifact["filename"].format(**values)
            except (KeyError, ValueError) as exc:
                raise ArtifactLayoutError(f"{label} has an unsupported filename placeholder") from exc
            if Path(filename).name in reserved:
                raise ArtifactLayoutError(
                    f"{label} filename is reserved for bundle metadata: {filename}"
                )

    def root(self, name: str) -> Path:
        try:
            return self.roots[name]
        except KeyError as exc:
            raise ArtifactLayoutError(f"profile has no artifact root: {name}") from exc

    def evidence_path(self, name: str) -> Path:
        if name == "result":
            return self.result_file
        try:
            relative = self.evidence[name]
        except KeyError as exc:
            raise ArtifactLayoutError(f"profile has no evidence file: {name}") from exc
        return self._path(relative, f"evidence_files.{name}", base=self.bundle_root)

    def task(self, issue_key: str) -> Path:
        return self.root("tasks") / f"{issue_key}.md"

    def generated(self, name: str, issue_key: str) -> Path:
        """Resolve a named profile-declared generated artifact."""
        config = self.profile.get("artifacts", {}).get("generated", {})
        if not isinstance(config, dict) or name not in config:
            raise ArtifactLayoutError(f"profile has no generated artifact: {name}")
        return self._artifact_path(
            config[name],
            issue_key=issue_key,
            field=f"artifacts.generated.{name}",
        )

    def review(self, issue_key: str) -> Path:
        review_stage = self._review_stage()
        aggregate = review_stage.get("aggregate", {})
        if isinstance(aggregate, dict) and isinstance(aggregate.get("artifact"), dict):
            return self._artifact_path(
                aggregate["artifact"],
                issue_key=issue_key,
                field="workflow.stages.review.aggregate.artifact",
            )
        return self.root("reviews") / f"{issue_key}-review.md"

    def reviewer_paths(self, issue_key: str) -> dict[str, Path]:
        """Resolve every configured reviewer result artifact for an issue.

        Reviewer output paths are declarations in the profile, not a filename
        convention for adapters to rediscover. Validate collisions here so the
        review dispatcher and publication adapters consume the same paths.
        """
        review_stage = self._review_stage()
        reviewers = review_stage.get("reviewers", [])
        if not isinstance(reviewers, list):
            raise ArtifactLayoutError("workflow.stages.review.reviewers must be a list")
        aggregate_path = self.review(issue_key).resolve()
        paths: dict[str, Path] = {}
        seen = {aggregate_path}
        for reviewer in reviewers:
            if not isinstance(reviewer, dict) or not isinstance(reviewer.get("id"), str):
                raise ArtifactLayoutError("each review reviewer requires an id")
            reviewer_id = reviewer["id"]
            if reviewer_id in paths:
                raise ArtifactLayoutError(f"duplicate reviewer: {reviewer_id}")
            try:
                artifact = reviewer["result"]["artifact"]
            except (KeyError, TypeError) as exc:
                raise ArtifactLayoutError(f"reviewer {reviewer_id} requires result.artifact") from exc
            destination = self._artifact_path(
                artifact,
                issue_key=issue_key,
                reviewer_id=reviewer_id,
                field=f"reviewer {reviewer_id} result.artifact",
            ).resolve()
            if destination in seen:
                raise ArtifactLayoutError(f"unsafe or shared reviewer output: {destination}")
            seen.add(destination)
            paths[reviewer_id] = destination
        return paths

    def _review_stage(self) -> dict[str, Any]:
        workflow = self.profile.get("workflow")
        stages = workflow.get("stages") if isinstance(workflow, dict) else None
        if not isinstance(stages, list):
            raise ArtifactLayoutError("profile workflow requires stages to resolve review artifacts")
        for candidate in stages:
            if isinstance(candidate, dict) and candidate.get("id") == "review":
                return candidate
        raise ArtifactLayoutError("profile has no review stage")

    def _artifact_path(
        self,
        artifact: Any,
        *,
        issue_key: str,
        reviewer_id: str | None = None,
        field: str,
    ) -> Path:
        if not isinstance(artifact, dict):
            raise ArtifactLayoutError(f"{field} must be a mapping")
        root_name = artifact.get("root")
        filename_template = artifact.get("filename")
        try:
            root = self.root(root_name)
        except (ArtifactLayoutError, TypeError) as exc:
            raise ArtifactLayoutError(f"{field}.root is invalid") from exc
        if not isinstance(filename_template, str) or not filename_template:
            raise ArtifactLayoutError(f"{field}.filename must be a non-empty string")
        values = {"issue_key": issue_key}
        if reviewer_id is not None:
            values["reviewer_id"] = reviewer_id
        try:
            filename = filename_template.format(**values)
        except (KeyError, ValueError) as exc:
            raise ArtifactLayoutError(f"{field}.filename has an unsupported placeholder") from exc
        if Path(filename).name != filename or filename in {"", ".", ".."}:
            raise ArtifactLayoutError(f"unsafe artifact filename: {filename}")
        destination = (root / filename).resolve()
        if not destination.is_relative_to(self.workspace_root):
            raise ArtifactLayoutError(f"artifact path escapes workspace: {field}")
        return destination

    def _path(self, value: Any, field: str, base: Path | None = None) -> Path:
        if not isinstance(value, str) or not value:
            raise ArtifactLayoutError(f"profile requires a non-empty {field}")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ArtifactLayoutError(f"artifact path must stay within workspace: {field}")
        return (base or self.workspace_root) / path
