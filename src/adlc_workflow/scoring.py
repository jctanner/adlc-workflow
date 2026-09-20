"""Deterministic profile-driven review scoring."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from .artifacts import ArtifactLayout
from .profiles import resource_path
from .reviews import review_plan


class ScoringError(RuntimeError):
    """Raised when configured scoring inputs cannot produce a verdict."""


_TOTAL_RE = re.compile(r"(?i)\btotal\b[^\n|]*\|?\s*\**(\d+)\s*/\s*(\d+)")
_VERDICT_SCORE_RE = re.compile(
    r"(?i)\bverdict\b[^\n]*?\bscore\s*:\s*\**(\d+)\s*/\s*(\d+)"
)
_DIMENSION_RE = re.compile(r"^\s*\|[^|]+\|\s*\**(\d+)\s*/\s*\d+\s*\|")
_TOTAL_BARE_TABLE_RE = re.compile(
    r"(?im)^\s*\|\s*(?:\*\*)?total(?:\*\*)?\s*\|\s*(?:\*\*)?(\d+)(?:\*\*)?\s*\|"
)
_DIMENSION_HEADING_RE = re.compile(
    r"(?im)^#{2,6}\s+(?:\d+\.\s+)?[^\n]*?\bscore\s*:\s*(\d+)\b"
)


def _score_document(path: Path, *, expected_maximum: int | None = None) -> tuple[int, int, int]:
    content = path.read_text(encoding="utf-8")
    # The deterministic contract prefers an explicit total row. The scoring
    # reviewer also emits its required overall score in the verdict summary,
    # e.g. `Verdict: REVISE ... (score: 6/8)`. Accept that equivalent form
    # without mistaking the first individual dimension score for the total.
    match = _TOTAL_RE.search(content) or _VERDICT_SCORE_RE.search(content)
    if match:
        total, maximum = int(match.group(1)), int(match.group(2))
    else:
        bare_total = _TOTAL_BARE_TABLE_RE.search(content)
        if bare_total is None or expected_maximum is None:
            raise ScoringError(f"reviewer output has no parseable total: {path}")
        total, maximum = int(bare_total.group(1)), expected_maximum
    if total < 0 or maximum <= 0 or total > maximum:
        raise ScoringError(f"reviewer output has invalid total: {path}")
    dimension_scores = [
        int(match.group(1)) for line in content.splitlines()
        if "|" in line and "total" not in line.lower() and (match := _DIMENSION_RE.match(line))
    ]
    if not dimension_scores:
        dimension_scores = [int(match.group(1)) for match in _DIMENSION_HEADING_RE.finditer(content)]
    zero_count = sum(score == 0 for score in dimension_scores)
    return total, maximum, zero_count


def _condition_matches(expression: str, total: int, zero_count: int) -> bool:
    allowed = {"total": total, "zero_count": zero_count}
    try:
        return bool(eval(expression, {"__builtins__": {}}, allowed))
    except (SyntaxError, NameError, TypeError, ValueError) as exc:
        raise ScoringError(f"unsupported scoring verdict expression: {expression}") from exc


def score_review(
    install_root: Path,
    workspace_root: Path,
    profile: dict[str, Any],
    issue_key: str,
    profile_path: str,
) -> dict[str, Any]:
    """Score configured reviewer input(s) and persist the public score record."""
    plan = review_plan(install_root, workspace_root, profile, issue_key, profile_path)
    review = next(stage for stage in profile["workflow"]["stages"] if stage.get("id") == "review")
    scoring = review.get("scoring")
    if not isinstance(scoring, dict):
        raise ScoringError("review requires scoring configuration")
    verdict_engine = scoring.get("verdict")
    if not isinstance(verdict_engine, str) or not verdict_engine.startswith("core:"):
        raise ScoringError("review.scoring.verdict must name a core verdict engine")
    inputs = scoring.get("inputs")
    if not isinstance(inputs, list) or not inputs or not all(isinstance(item, str) and item for item in inputs):
        raise ScoringError("review.scoring.inputs must be a non-empty list")
    assignments = {item["reviewer_id"]: item for item in plan["assignments"]}
    unknown = set(inputs) - set(assignments)
    if unknown:
        raise ScoringError(f"review.scoring.inputs reference unknown reviewers: {sorted(unknown)}")
    rubric_ref = scoring.get("rubric", {}).get("path") if isinstance(scoring.get("rubric"), dict) else None
    rubric_path = resource_path(install_root, profile, rubric_ref, "workflow.stages.review.scoring.rubric.path")
    rubric = yaml.safe_load(rubric_path.read_text(encoding="utf-8"))
    if not isinstance(rubric, dict) or not isinstance(rubric.get("verdict", {}).get("rules"), list):
        raise ScoringError(f"rubric has no deterministic verdict rules: {rubric_path}")
    maximum = rubric.get("scoring", {}).get("total", {}).get("maximum") if isinstance(rubric.get("scoring"), dict) else None
    if not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0:
        raise ScoringError(f"rubric has no valid scoring.total.maximum: {rubric_path}")

    scores = {}
    for reviewer_id in inputs:
        path = Path(assignments[reviewer_id]["output_path"])
        if not path.is_file() or not path.read_text(encoding="utf-8").strip():
            raise ScoringError(f"scoring input is missing or empty: {path}")
        total, parsed_maximum, zero_count = _score_document(path, expected_maximum=maximum)
        scores[reviewer_id] = {"total": total, "maximum": parsed_maximum, "zero_count": zero_count}

    primary = scores[inputs[0]]
    verdict_rule = next(
        (rule for rule in rubric["verdict"]["rules"] if _condition_matches(rule.get("when", "false"), primary["total"], primary["zero_count"])),
        None,
    )
    if verdict_rule is None:
        raise ScoringError("no rubric verdict rule matched reviewer scores")
    labels = rubric.get("verdict", {}).get("labels", {})
    verdict = verdict_rule.get("id")
    result = {
        "schema_version": 1,
        "issue_key": issue_key,
        "rubric": rubric.get("id"),
        "verdict_engine": verdict_engine,
        "inputs": inputs,
        "scores": scores,
        "total": primary["total"],
        "maximum": primary["maximum"],
        "zero_count": primary["zero_count"],
        "verdict": verdict,
        "label": labels.get(verdict),
        "needs_attention": bool(verdict_rule.get("needs_attention", True)),
        "verdict_rule": verdict_rule.get("when"),
    }
    layout = ArtifactLayout(workspace_root, profile)
    destination = layout.generated("score", issue_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return result
