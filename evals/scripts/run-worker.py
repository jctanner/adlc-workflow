#!/usr/bin/env python3
"""Container-only controller-style refinement worker launcher."""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/opt/adlc-evals")
from eval_lib.production_bridge import execute_worker  # noqa: E402
from eval_lib.records import EvalError  # noqa: E402


def main() -> int:
    plugin = Path(os.environ.get("CLAUDE_PLUGIN_ROOT", "/home/evaluator/.claude/plugins/adlc-workflow"))
    workspace = Path("/workspace")
    input_dir = workspace / ".adlc-eval" / "input"
    result_path = workspace / ".adlc-eval" / "container-result.json"
    try:
        result = execute_worker(plugin, workspace, input_dir, os.environ["ADLC_EVAL_MODEL"],
                                int(os.environ.get("ADLC_EVAL_TIMEOUT_SECONDS", "900")))
    except (EvalError, OSError, ValueError, KeyError) as exc:
        result = {"schema_version": 1, "status": "setup_failed", "error": str(exc), "error_type": type(exc).__name__}
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if result.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
