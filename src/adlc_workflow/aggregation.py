"""Profile-driven aggregation of completed reviewer artifacts."""

from __future__ import annotations

from pathlib import Path

from .reviews import review_plan


class AggregationError(RuntimeError):
    """Raised when configured review inputs cannot be aggregated."""


def aggregate_review(
    install_root: Path,
    workspace_root: Path,
    profile: dict,
    issue_key: str,
    profile_path: str,
) -> Path:
    """Render the configured reviewer inputs into the profile aggregate path."""
    plan = review_plan(install_root, workspace_root, profile, issue_key, profile_path)
    aggregate = plan["aggregate"]
    schema_path = Path(aggregate["schema"])
    if not schema_path.is_file():
        raise AggregationError(f"aggregate schema does not exist: {schema_path}")

    assignments = {item["reviewer_id"]: item for item in plan["assignments"]}
    sections: list[str] = []
    for reviewer_id in aggregate["inputs"]:
        assignment = assignments[reviewer_id]
        path = Path(assignment["output_path"])
        if not path.is_file():
            raise AggregationError(f"reviewer output is missing: {path}")
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            raise AggregationError(f"reviewer output is empty: {path}")
        sections.append(f"## Reviewer: {reviewer_id}\n\n{content}")

    renderer = aggregate["renderer"]
    output = (
        f"# Aggregated review: {issue_key}\n\n"
        f"Renderer: `{renderer}`\n"
        f"Schema: `{schema_path}`\n\n"
        "The sections below are the configured reviewer inputs, in profile order.\n\n"
        + "\n\n".join(sections)
        + "\n"
    )
    destination = Path(plan["aggregate_path"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(output, encoding="utf-8")
    temporary.replace(destination)
    return destination
