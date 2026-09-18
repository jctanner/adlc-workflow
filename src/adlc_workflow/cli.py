"""CLI for the ADLC task protocol."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .core import WorkflowCore, WorkflowError


def _json_file(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise WorkflowError(f"expected JSON object in {path}")
    return value


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advance the ADLC workflow core")
    parser.add_argument("--install-root", help="installed workflow package root")
    parser.add_argument("--workspace", help="runtime workspace for artifacts and state")
    parser.add_argument("--project-root", help="deprecated alias for both roots")
    parser.add_argument("--state-root")
    parser.add_argument("--profile", default="config/rhai-feature-creator.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--request", required=True)
    for name in ("advance", "status"):
        command = commands.add_parser(name)
        command.add_argument("--run", required=True)
    submit = commands.add_parser("submit")
    submit.add_argument("--run", required=True)
    submit.add_argument("--task", required=True)
    submit.add_argument("--result", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        legacy_root = args.project_root
        install_root = args.install_root or legacy_root or "."
        workspace_root = args.workspace or legacy_root or "."
        core = WorkflowCore(install_root, workspace_root, args.state_root)
        if args.command == "start":
            output = core.start(_json_file(args.request), args.profile)
        elif args.command == "advance":
            output = core.advance(args.run)
        elif args.command == "status":
            output = core.status(args.run)
        else:
            output = core.submit(args.run, args.task, _json_file(args.result))
    except (WorkflowError, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
