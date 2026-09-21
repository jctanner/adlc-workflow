#!/usr/bin/env python3
"""Render a saved ADLC evaluation experiment as a standalone HTML report.

This consumes only saved evaluation records. It never starts containers or
invokes a model, and it intentionally renders incomplete experiments so a
failed deterministic gate is visible instead of looking like missing data.
"""

from __future__ import annotations

import argparse
import html
import json
from collections import Counter
from pathlib import Path
from typing import Any


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"expected an object in {path}")
    return value


def _text(value: Any) -> str:
    return html.escape("—" if value is None else str(value))


def _money(value: Any) -> str:
    return f"${float(value):,.4f}" if isinstance(value, (int, float)) else "—"


def _number(value: Any, suffix: str = "") -> str:
    return f"{float(value):,.1f}{suffix}" if isinstance(value, (int, float)) else "—"


def _badge(value: str, good: bool | None = None) -> str:
    if good is None:
        good = value in {"pass", "succeeded", "eligible", "complete", "A", "B", "tie"}
    tone = "good" if good else "bad" if value in {"fail", "failed", "excluded", "judge_error", "timed_out"} else "neutral"
    return f'<span class="badge {tone}">{_text(value)}</span>'


def _trial(root: Path, execution_path: Path) -> dict[str, Any]:
    trial_root = execution_path.parent
    execution = _json(execution_path)
    checks_path = trial_root / "checks.json"
    checks = _json(checks_path) if checks_path.is_file() else {"eligible": False, "checks": []}
    issue_key = str(execution.get("case_id", "unknown"))
    request_path = trial_root / "input" / "request.json"
    if request_path.is_file():
        request = _json(request_path)
        source = request.get("kwargs", {}).get("source_issues", {})
        if isinstance(source, dict) and source:
            issue_key = str(next(iter(source)))
    strategies = sorted((trial_root / "workspace" / "artifacts" / "rhai-feature-tasks").glob("*.md"))
    return {"root": trial_root, "execution": execution, "checks": checks, "issue_key": issue_key, "strategies": strategies}


def _strategy(strategy: Path) -> str:
    content = strategy.read_text(encoding="utf-8", errors="replace")
    return (
        '<details class="strategy"><summary>Generated strategy — '
        f'{_text(strategy.name)} ({len(content):,} bytes)</summary><pre>{html.escape(content)}</pre></details>'
    )


def _check_table(checks: dict[str, Any]) -> str:
    rows = []
    for check in checks.get("checks", []):
        if not isinstance(check, dict):
            continue
        status = str(check.get("status", "unknown"))
        rows.append(f"<tr><td>{_text(check.get('id'))}</td><td>{_badge(status)}</td><td>{_text(check.get('detail'))}</td></tr>")
    return "<table><thead><tr><th>Gate</th><th>Status</th><th>Detail</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"


def _trial_card(trial: dict[str, Any]) -> str:
    execution = trial["execution"]
    checks = trial["checks"]
    usage = execution.get("usage", {}) if isinstance(execution.get("usage"), dict) else {}
    candidate = str(execution.get("candidate", "?"))
    model = execution.get("model_requested")
    eligible = bool(checks.get("eligible"))
    strategies = "".join(_strategy(path) for path in trial["strategies"]) or "<p>No strategy artifact was written.</p>"
    return f"""
    <article class="trial">
      <header><div><p class="eyebrow">Candidate {_text(candidate)} · {_text(trial['issue_key'])}</p><h3>{_text(model)}</h3></div>
      <div>{_badge(str(execution.get('status', 'unknown')))} {_badge('eligible' if eligible else 'excluded', eligible)}</div></header>
      <dl class="metrics">
        <div><dt>Duration</dt><dd>{_number(execution.get('container_duration_seconds'), ' s')}</dd></div>
        <div><dt>Cost</dt><dd>{_money(usage.get('cost_usd'))}</dd></div>
        <div><dt>Output tokens</dt><dd>{_text(usage.get('output_tokens'))}</dd></div>
        <div><dt>Cache-read tokens</dt><dd>{_text(usage.get('cache_read_input_tokens'))}</dd></div>
      </dl>
      <h4>Deterministic gates</h4>
      {_check_table(checks)}
      <h4>Artifact</h4>{strategies}
    </article>"""


def _mapped_preference(preference: Any, mapping: dict[str, Any]) -> str:
    value = str(preference or "unknown")
    return str(mapping.get(value, value))


def _presentation_rationale(root: Path, reference: Any) -> str:
    if not isinstance(reference, str):
        return ""
    presentation_path = root / reference
    if not presentation_path.is_file():
        return f"<p class=\"subtle\">Missing presentation record: {_text(reference)}</p>"
    presentation = _json(presentation_path)
    mapping_path = presentation_path.parent / "mapping.private.json"
    mapping = _json(mapping_path) if mapping_path.is_file() else {}
    judgment = presentation.get("judgment")
    label = presentation_path.parent.name
    if not isinstance(judgment, dict):
        return f"<article class=\"presentation\"><h4>{_text(label)}</h4><p>{_badge(str(presentation.get('status', 'unknown')))} {_text(presentation.get('error'))}</p></article>"
    preference = judgment.get("preference")
    mapped = _mapped_preference(preference, mapping)
    dimensions = judgment.get("dimensions", [])
    rows = []
    for dimension in dimensions if isinstance(dimensions, list) else []:
        if not isinstance(dimension, dict):
            continue
        evidence = dimension.get("evidence", [])
        evidence_lines = []
        for item in evidence if isinstance(evidence, list) else []:
            if not isinstance(item, dict):
                continue
            document = _mapped_preference(item.get("document"), mapping)
            evidence_lines.append(
                f"<li><strong>{_text(document)}</strong> · {_text(item.get('section'))}: “{_text(item.get('excerpt'))}”</li>"
            )
        evidence_html = f"<details><summary>Evidence</summary><ul>{''.join(evidence_lines)}</ul></details>" if evidence_lines else ""
        dimension_preference = _mapped_preference(dimension.get("preference"), mapping)
        rows.append(
            f"<tr><td>{_text(dimension.get('id'))}</td><td>{_badge(dimension_preference)}</td>"
            f"<td>{_text(dimension.get('rationale'))}{evidence_html}</td></tr>"
        )
    mapping_text = f"X = {_text(mapping.get('X'))}; Y = {_text(mapping.get('Y'))}"
    return f"""
    <article class=\"presentation\"><h4>{_text(label)} · {mapping_text}</h4>
    <p><strong>Overall preference:</strong> {_badge(mapped)}<br>{_text(judgment.get('rationale'))}</p>
    <table><thead><tr><th>Dimension</th><th>Preferred candidate</th><th>Judge rationale</th></tr></thead>
    <tbody>{''.join(rows)}</tbody></table></article>"""


def _pair_rationale(root: Path, pair: dict[str, Any]) -> str:
    presentations = "".join(_presentation_rationale(root, pair.get(key)) for key in ("first", "second"))
    if not presentations:
        return ""
    return f"<details class=\"judge-details\" open><summary>Why the judge selected {_text(pair.get('outcome'))}</summary>{presentations}</details>"


def _evaluation(root: Path, evaluation_id: str | None = None) -> str:
    evaluations = ([root / "evaluations" / evaluation_id / "evaluation.json"] if evaluation_id
                   else sorted(root.glob("evaluations/*/evaluation.json")))
    if evaluation_id and not evaluations[0].is_file():
        raise ValueError(f"evaluation does not exist: {evaluation_id}")
    if not evaluations:
        return """
        <section class="callout warning"><h2>Judging did not run</h2>
        <p>No evaluation record exists. The experiment stopped before the blinded judge stage, normally because at least one deterministic gate excluded the pair.</p></section>"""
    sections = []
    for path in evaluations:
        record = _json(path)
        pairs = record.get("pairs", []) if isinstance(record.get("pairs"), list) else []
        outcomes = Counter(str(pair.get("outcome", "unknown")) for pair in pairs if isinstance(pair, dict))
        rows = "".join(f"<tr><td>{_text(pair.get('case_id'))}</td><td>{_text(pair.get('repetition'))}</td><td>{_badge(str(pair.get('outcome', 'unknown')))}</td><td>{_text(pair.get('reason', ''))}</td></tr>" for pair in pairs if isinstance(pair, dict))
        rationales = "".join(_pair_rationale(root, pair) for pair in pairs if isinstance(pair, dict))
        summary = ", ".join(f"{name}: {count}" for name, count in sorted(outcomes.items())) or "no pairs"
        sections.append(f"""
        <section class="judge"><h2>Blinded judge: {_text(record.get('evaluation_id'))}</h2>
        <p><strong>Requested model:</strong> {_text(record.get('judge_model_requested'))} · <strong>Outcomes:</strong> {_text(summary)}</p>
        <table><thead><tr><th>Case</th><th>Repetition</th><th>Outcome</th><th>Reason</th></tr></thead><tbody>{rows}</tbody></table>{rationales}</section>""")
    return "".join(sections)


def render(root: Path, evaluation_id: str | None = None) -> str:
    root = root.resolve()
    experiment = _json(root / "experiment.json")
    config = experiment.get("effective_config", {}) if isinstance(experiment.get("effective_config"), dict) else {}
    candidate_models = config.get("candidates", {}) if isinstance(config.get("candidates"), dict) else {}
    trials = [_trial(root, path) for path in sorted(root.glob("trials/*/repetition-*/*/execution.json"))]
    succeeded = sum(item["execution"].get("status") == "succeeded" for item in trials)
    eligible = sum(bool(item["checks"].get("eligible")) for item in trials)
    cards = "".join(_trial_card(item) for item in trials) or "<p>No candidate trials were collected.</p>"
    model_list = ", ".join(f"{name}: {value.get('model', '—')}" for name, value in sorted(candidate_models.items()) if isinstance(value, dict))
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>ADLC evaluation — {_text(experiment.get('experiment_id'))}</title>
<style>
:root {{ color-scheme: light; --ink:#172033; --muted:#5f6c80; --paper:#f5f7fb; --panel:#fff; --line:#d9e0ec; --blue:#275dad; --green:#087443; --red:#b42318; --amber:#9a6700; }}
* {{ box-sizing:border-box }} body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif }}
main {{ max-width:1440px; margin:auto; padding:42px 28px 72px }} h1 {{ font-size:clamp(28px,4vw,46px); line-height:1.1; margin:6px 0 12px }} h2 {{ margin-top:36px }} h3 {{ margin:0; font-size:22px }} h4 {{ margin:24px 0 9px }} .eyebrow {{ color:var(--blue); font-size:12px; font-weight:700; letter-spacing:.09em; margin:0; text-transform:uppercase }} .subtle {{ color:var(--muted) }}
.summary,.trial,.judge,.callout {{ background:var(--panel); border:1px solid var(--line); border-radius:14px; padding:22px; box-shadow:0 2px 8px #1720330a }} .summary-grid,.trials {{ display:grid; gap:18px }} .summary-grid {{ grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); margin-top:24px }} .summary-grid div {{ border-left:3px solid var(--blue); padding-left:12px }} .summary-grid strong {{ display:block; font-size:23px }} .trials {{ grid-template-columns:repeat(auto-fit,minmax(480px,1fr)) }} .trial header {{ align-items:flex-start; display:flex; gap:16px; justify-content:space-between }}
.badge {{ border-radius:999px; display:inline-block; font-size:12px; font-weight:700; padding:3px 9px; white-space:nowrap }} .good {{ background:#dcfae6; color:var(--green) }} .bad {{ background:#ffebe9; color:var(--red) }} .neutral {{ background:#edf1f7; color:#45546a }} .warning {{ background:#fff9e8; border-color:#f4d88d }}
.metrics {{ display:grid; gap:10px; grid-template-columns:repeat(2,minmax(0,1fr)); margin:20px 0 }} .metrics div {{ background:#f8faff; border-radius:8px; padding:9px }} dt {{ color:var(--muted); font-size:12px }} dd {{ font-size:17px; font-weight:650; margin:2px 0 0 }} table {{ border-collapse:collapse; font-size:13px; width:100% }} th,td {{ border-bottom:1px solid var(--line); padding:8px; text-align:left; vertical-align:top }} th {{ color:var(--muted); font-size:11px; text-transform:uppercase }} .strategy,.judge-details {{ margin-top:16px }} .presentation {{ background:#f8faff; border:1px solid var(--line); border-radius:10px; margin:14px 0; padding:14px }} .presentation h4 {{ margin:0 0 8px }} .presentation ul {{ margin:8px 0; padding-left:22px }} summary {{ color:var(--blue); cursor:pointer; font-weight:650 }} pre {{ background:#101827; border-radius:8px; color:#e8eef9; max-height:620px; overflow:auto; padding:16px; white-space:pre-wrap; word-break:break-word }}
@media(max-width:560px) {{ main {{ padding:26px 14px }} .trials {{ grid-template-columns:1fr }} .trial {{ padding:15px }} }}
</style></head><body><main>
<p class="eyebrow">Saved, offline evaluation report</p><h1>{_text(experiment.get('name'))}</h1>
<p class="subtle">Experiment <code>{_text(experiment.get('experiment_id'))}</code> · prepared {_text(experiment.get('prepared_at'))}</p>
<section class="summary"><p><strong>Candidates:</strong> {_text(model_list)}</p><p><strong>Invocation:</strong> {_text(config.get('target', {}).get('invocation') if isinstance(config.get('target'), dict) else None)}</p>
<div class="summary-grid"><div><span>Collected trials</span><strong>{len(trials)}</strong></div><div><span>Succeeded</span><strong>{succeeded}</strong></div><div><span>Eligible for judging</span><strong>{eligible}</strong></div><div><span>Cases / repetitions</span><strong>{_text(len(config.get('cases', [])) if isinstance(config.get('cases'), list) else 0)} / {_text(config.get('repetitions'))}</strong></div></div></section>
{_evaluation(root, evaluation_id)}
<h2>Candidate execution</h2><section class="trials">{cards}</section>
</main></body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--experiment", required=True, type=Path, help="Saved evals/results/<experiment> directory")
    parser.add_argument("--evaluation-id", help="Render one blinded judge evaluation instead of every saved evaluation")
    parser.add_argument("--output", type=Path, help="HTML destination (default: <experiment>/report.html)")
    args = parser.parse_args()
    output = args.output.resolve() if args.output else args.experiment.resolve() / "report.html"
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(render(args.experiment, args.evaluation_id), encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
