"""Command line for the standalone evaluation directory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .checks import check_trial
from .collection import collect
from .config import load_experiment
from .judging import judge
from .records import EvalError, read_json
from .reporting import report
from .snapshots import prepare


def _print(value: object) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _check(root: Path) -> dict:
    config = load_experiment(root / "resources" / "experiment.yaml")
    values = []
    for path in sorted(root.glob("trials/*/repetition-*/*")):
        if not (path / "execution.json").is_file():
            continue
        candidate = path.name
        values.append({"trial": str(path.relative_to(root)), **check_trial(root, path, config.candidates[candidate].__dict__)})
    return {"schema_version": 1, "trials": values, "eligible": sum(item["eligible"] for item in values), "total": len(values)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--config", required=True, type=Path)
    prepared = sub.add_parser("prepare")
    prepared.add_argument("--config", required=True, type=Path)
    prepared.add_argument("--experiment-id", required=True)
    prepared.add_argument("--results-root", type=Path, default=Path(__file__).resolve().parents[1] / "results")
    collected = sub.add_parser("collect")
    collected.add_argument("--experiment", required=True, type=Path)
    collected.add_argument("--candidate", choices=("A", "B"))
    collected.add_argument("--dry-run", action="store_true")
    checked = sub.add_parser("check")
    checked.add_argument("--experiment", required=True, type=Path)
    judged = sub.add_parser("judge")
    judged.add_argument("--experiment", required=True, type=Path)
    judged.add_argument("--evaluation-id", required=True)
    judged.add_argument("--judge-model")
    judged.add_argument("--prompt-file", type=Path)
    reported = sub.add_parser("report")
    reported.add_argument("--experiment", required=True, type=Path)
    reported.add_argument("--evaluation-id", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            config = load_experiment(args.config)
            result = {"status": "valid", "name": config.name, "cases": [case.id for case in config.cases],
                      "trials": len(config.cases) * config.repetitions * 2, "judge_calls": len(config.cases) * config.repetitions * (2 if config.order_swap else 1)}
            code = 0
        elif args.command == "prepare":
            result = {"status": "prepared", "experiment": str(prepare(load_experiment(args.config), args.results_root, args.experiment_id))}
            code = 0
        elif args.command == "collect":
            result = {"trials": collect(args.experiment, args.candidate, args.dry_run)}
            code = 0 if all(item["status"] in {"planned", "succeeded", "skipped"} for item in result["trials"]) else 1
        elif args.command == "check":
            result = _check(args.experiment.resolve())
            code = 0 if result["eligible"] == result["total"] else 1
        elif args.command == "judge":
            result = judge(args.experiment, args.evaluation_id, args.judge_model, args.prompt_file)
            code = 0
        else:
            result = report(args.experiment, args.evaluation_id)
            code = 0
    except (EvalError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    _print(result)
    return code
