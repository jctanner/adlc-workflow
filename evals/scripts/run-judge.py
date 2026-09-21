#!/usr/bin/env python3
"""Container-only blind pairwise judge launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from eval_lib.judge_output import parse_judgment_json  # noqa: E402


def main() -> int:
    input_root = Path("/judge/input")
    output_root = Path("/judge/output")
    output_root.mkdir(parents=True, exist_ok=True)
    prompt = (input_root / "prompt.txt").read_text(encoding="utf-8")
    model = os.environ["ADLC_EVAL_MODEL"]
    command = ["claude", "--model", model, "--output-format", "stream-json", "--verbose", "-p", prompt]
    timeout = int(os.environ.get("ADLC_EVAL_TIMEOUT_SECONDS", "300"))
    try:
        result = subprocess.run(command, cwd="/judge", text=True, capture_output=True, timeout=timeout)
        (output_root / "stdout.jsonl").write_text(result.stdout, encoding="utf-8")
        (output_root / "stderr.log").write_text(result.stderr, encoding="utf-8")
        terminal = None
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                terminal = event
        if result.returncode or not isinstance(terminal, dict) or terminal.get("is_error"):
            payload = {"schema_version": 1, "status": "failed", "returncode": result.returncode,
                       "error": (terminal or {}).get("result") if isinstance(terminal, dict) else "missing terminal Claude result"}
        else:
            raw = terminal.get("result", "")
            try:
                judgment = parse_judgment_json(raw)
            except (TypeError, ValueError) as exc:
                payload = {"schema_version": 1, "status": "invalid", "error": f"judge did not return JSON: {exc}", "raw_result": raw}
            else:
                payload = {"schema_version": 1, "status": "succeeded", "judgment": judgment}
    except subprocess.TimeoutExpired as exc:
        (output_root / "stdout.jsonl").write_text(exc.stdout or "", encoding="utf-8")
        (output_root / "stderr.log").write_text((exc.stderr or "") + "\njudge timed out", encoding="utf-8")
        payload = {"schema_version": 1, "status": "timed_out"}
    (output_root / "judge-result.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(payload, sort_keys=True), flush=True)
    return 0 if payload["status"] == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
