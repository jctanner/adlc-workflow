"""CLI for the ADLC task protocol."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .controller import ControllerError, ControllerOptions, HandoffController
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
    parser.add_argument("--profile", default=os.environ.get("ADLC_PROFILE", "config/rhai-feature-creator.yaml"))
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
    handoff = commands.add_parser("handoff", help="drive the core and delegate bounded LLM work to Claude")
    handoff.add_argument("issue_keys", nargs="*")
    handoff.add_argument("--profile", dest="handoff_profile", help="profile name or install-relative path")
    handoff.add_argument("--install-root", dest="handoff_install_root")
    handoff.add_argument("--workspace", dest="handoff_workspace")
    handoff.add_argument("--state-root", dest="handoff_state_root")
    handoff.add_argument("--mode", choices=("production", "local", "eval"), default="local")
    handoff.add_argument("--identity", default="adlc-handoff")
    handoff.add_argument("--model")
    handoff.add_argument("--claude-bin", default="claude")
    handoff.add_argument("--task-timeout-seconds", type=float)
    handoff.add_argument("--reviewer-parallelism", type=int)
    handoff.add_argument("--max-attempts", type=int)
    handoff.add_argument("--item-parallelism", type=int, default=1,
                         help="concurrently process up to this many batch items (default: 1)")
    handoff.add_argument("--batch-size", type=int)
    handoff.add_argument("--batch-offset", type=int, default=0)
    handoff.add_argument("--dangerously-skip-permissions", action="store_true")
    handoff.add_argument("--json", action="store_true", help="emit normalized controller and Claude events as JSONL")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "handoff":
            install_root = args.handoff_install_root or args.install_root or args.project_root or "."
            workspace_root = args.handoff_workspace or args.workspace or args.project_root or "."
            options = ControllerOptions(
                profile=args.handoff_profile or args.profile,
                issue_keys=tuple(args.issue_keys),
                mode=args.mode,
                identity=args.identity,
                model=args.model,
                timeout_seconds=args.task_timeout_seconds,
                reviewer_parallelism=args.reviewer_parallelism,
                max_attempts=args.max_attempts,
                item_parallelism=args.item_parallelism,
                batch_size=args.batch_size,
                batch_offset=args.batch_offset,
                state_root=args.handoff_state_root,
                claude_bin=args.claude_bin,
                dangerously_skip_permissions=args.dangerously_skip_permissions,
                output="json" if args.json else "human",
            )
            output = HandoffController(install_root, workspace_root, options).run()
            return 1 if output.get("kind") == "blocked" else 0
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
    except (WorkflowError, ControllerError, OSError, json.JSONDecodeError) as exc:
        error = {"type": "controller.error", "error": str(exc)}
        print(json.dumps(error) if getattr(args, "json", False) else error["error"], file=sys.stderr)
        return 2
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
