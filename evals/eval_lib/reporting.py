"""Describe saved A/B experiment records without running models."""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .records import EvalError, read_json, write_json


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def report(root: Path, evaluation_id: str) -> dict[str, Any]:
    root = root.resolve()
    evaluation = root / "evaluations" / evaluation_id / "evaluation.json"
    if not evaluation.is_file():
        raise EvalError(f"evaluation does not exist: {evaluation_id}")
    judged = read_json(evaluation)
    trials = sorted(root.glob("trials/*/repetition-*/*/execution.json"))
    by_candidate: dict[str, dict[str, Any]] = defaultdict(lambda: {"attempted": 0, "succeeded": 0, "comparison_eligible": 0,
                                                                      "durations": [], "costs": [], "cache_read_tokens": []})
    for path in trials:
        value = read_json(path)
        candidate = str(value.get("candidate", "unknown"))
        target = by_candidate[candidate]
        target["attempted"] += 1
        if value.get("status") == "succeeded":
            target["succeeded"] += 1
        if value.get("comparison_eligible"):
            target["comparison_eligible"] += 1
        duration = value.get("container_duration_seconds")
        if isinstance(duration, (int, float)):
            target["durations"].append(float(duration))
        usage = value.get("usage", {})
        if isinstance(usage, dict):
            if isinstance(usage.get("cost_usd"), (int, float)):
                target["costs"].append(float(usage["cost_usd"]))
            if isinstance(usage.get("cache_read_input_tokens"), (int, float)):
                target["cache_read_tokens"].append(float(usage["cache_read_input_tokens"]))
    candidates = {}
    for name, value in by_candidate.items():
        candidates[name] = {key: item for key, item in value.items() if key not in {"durations", "costs", "cache_read_tokens"}}
        candidates[name].update({"duration_median_seconds": _median(value["durations"]),
                                 "duration_range_seconds": [min(value["durations"]), max(value["durations"])] if value["durations"] else None,
                                 "cost_median_usd": _median(value["costs"]),
                                 "cache_read_tokens_median": _median(value["cache_read_tokens"])})
    outcomes = Counter(str(pair.get("outcome", "unknown")) for pair in judged.get("pairs", []))
    decisive = outcomes["A"] + outcomes["B"]
    comparison = {"schema_version": 1, "evaluation_id": evaluation_id, "candidate_trials": candidates,
                  "pair_outcomes": dict(sorted(outcomes.items())), "decisive_win_fraction": {
                      "A": {"numerator": outcomes["A"], "denominator": decisive, "value": outcomes["A"] / decisive if decisive else None},
                      "B": {"numerator": outcomes["B"], "denominator": decisive, "value": outcomes["B"] / decisive if decisive else None},
                  }, "pairs": judged.get("pairs", [])}
    out_dir = root / "evaluations" / evaluation_id
    write_json(out_dir / "comparison.json", comparison)
    lines = [f"# ADLC A/B evaluation: {evaluation_id}", "", "## Pair outcomes", "",
             f"- A wins: {outcomes['A']}", f"- B wins: {outcomes['B']}", f"- Ties: {outcomes['tie']}",
             f"- Neither acceptable: {outcomes['neither']}", f"- Inconclusive: {outcomes['inconclusive']}",
             f"- Insufficient evidence: {outcomes['insufficient_evidence']}", f"- Judge errors: {outcomes['judge_error']}",
             f"- Excluded pairs: {outcomes['excluded']}", "", "## Candidate execution", ""]
    for name in sorted(candidates):
        value = candidates[name]
        lines.append(f"- {name}: attempted={value['attempted']}, succeeded={value['succeeded']}, eligible={value['comparison_eligible']}, duration median={value['duration_median_seconds']}s, cost median=${value['cost_median_usd']}")
    lines += ["", f"Decisive A fraction: {comparison['decisive_win_fraction']['A']['value']} ({outcomes['A']}/{decisive})", "",
              "This is descriptive evidence from the frozen cases and repetitions; it is not a statistical significance claim."]
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return comparison
