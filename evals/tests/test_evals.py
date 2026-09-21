"""Offline contracts for the standalone refinement A/B evaluator."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

from eval_lib.checks import check_trial
from eval_lib.config import load_experiment
from eval_lib.judge_output import parse_judgment_json
from eval_lib.judging import _reduce, _validate_judgment
from eval_lib.metrics import prohibited_activity, usage
from eval_lib.records import EvalError, sha256_file, write_json
from eval_lib.reporting import report
from eval_lib.snapshots import PROJECT_ROOT, prepare, schedule


EVAL_ROOT = Path(__file__).parents[1]


def config_at(root: Path, context_workspace: Path, *, repetitions: int = 1) -> Path:
    value = {
        "schema_version": 1, "name": "test", "suite": str(EVAL_ROOT / "suites/rhai-feature-refinement.yaml"),
        "target": {"worker": "skill:rhai-feature-refine-worker", "profile": "rhai-feature-creator", "invocation": "controller-worker"},
        "candidates": {"A": {"model": "model-a", "accepted_resolved_models": ["model-a"]},
                       "B": {"model": "model-b", "accepted_resolved_models": ["model-b"]}},
        "cases": [str(EVAL_ROOT / "cases/mcp-registry")], "repetitions": repetitions,
        "execution": {"order": "alternating", "worker_timeout_seconds": 10, "startup_timeout_seconds": 5, "container_image": "local-image"},
        "context": {"prepared_workspace": str(context_workspace)},
        "judge": {"model": "judge", "accepted_resolved_models": ["judge"], "prompt": str(EVAL_ROOT / "judges/refinement-pairwise.md"),
                  "timeout_seconds": 10, "evidence_max_bytes": 100000, "order_swap": True},
    }
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    return path


def prepared_context(root: Path) -> Path:
    context = root / "prepared" / ".context"
    destination = context / "architecture-context"
    destination.mkdir(parents=True)
    (destination / "LATEST_VERSION").write_text("rhoai-3.6-ea.2\n", encoding="utf-8")
    write_json(context / "context-manifest.json", {"schema_version": 1, "status": "prepared", "sources": [{
        "id": "rhai-architecture-context", "destination": ".context/architecture-context", "version_file": "LATEST_VERSION",
        "usage": "context/rhai-architecture-context/usage.md", "applied_overlays": []}]})
    return root / "prepared"


class EvalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.context = prepared_context(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_config_rejects_unknown_candidate_option(self) -> None:
        path = config_at(self.root, self.context)
        value = yaml.safe_load(path.read_text())
        value["candidates"]["A"]["unexpected"] = True
        path.write_text(yaml.safe_dump(value), encoding="utf-8")
        with self.assertRaises(EvalError):
            load_experiment(path)

    def test_prepare_freezes_plugin_and_content_digest(self) -> None:
        experiment = load_experiment(config_at(self.root, self.context, repetitions=2))
        result = prepare(experiment, self.root / "results", "run-1")
        self.assertTrue((result / "resources/plugin/src/adlc_workflow/controller.py").is_file())
        self.assertFalse((result / "resources/plugin/evals").exists())
        manifest = json.loads((result / "resources/file-manifest.json").read_text())
        self.assertEqual(4, len(json.loads((result / "schedule.json").read_text())["trials"]))
        self.assertTrue(manifest["plugin"]["sha256"])
        frozen = result / "resources/plugin/config/rhai-feature-creator.yaml"
        original = frozen.read_text(encoding="utf-8")
        frozen.write_text(original + "# changed\n", encoding="utf-8")
        self.assertNotEqual(manifest["plugin"]["sha256"], sha256_file(frozen))

    def test_metrics_uses_last_terminal_session_record(self) -> None:
        path = self.root / "events.jsonl"
        first = {"type": "claude.stream", "event": {"type": "result", "session_id": "one",
                 "modelUsage": {"model-a": {"costUSD": 1, "inputTokens": 10}}}}
        second = {"type": "claude.stream", "event": {"type": "result", "session_id": "one",
                  "modelUsage": {"model-a": {"costUSD": 2, "inputTokens": 20}}}}
        path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
        self.assertEqual(2, usage(path)["cost_usd"])
        self.assertEqual(20, usage(path)["input_tokens"])

    def test_bounded_worker_allows_plugin_helpers_but_rejects_orchestration(self) -> None:
        path = self.root / "events.jsonl"
        def event(command: str) -> dict:
            return {"type": "claude.stream", "event": {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "name": "Bash", "input": {"command": command}}]}}}
        path.write_text("\n".join(json.dumps(item) for item in (
            event("/home/evaluator/.claude/plugins/adlc-workflow/scripts/adlc-template-path --profile config/rhai-feature-creator.yaml"),
            event("$CLAUDE_PLUGIN_ROOT/scripts/adlc-artifact-path task RHAIRFE-1"),
        )) + "\n", encoding="utf-8")
        self.assertEqual([], prohibited_activity(path))
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event("$CLAUDE_PLUGIN_ROOT/scripts/adlc-workflow advance --run run-1")) + "\n")
        self.assertEqual(1, len(prohibited_activity(path)))

    def test_checks_validate_source_and_model(self) -> None:
        trial = self.root / "trial"
        input_dir = trial / "input"
        workspace = trial / "workspace"
        input_dir.mkdir(parents=True)
        task = {"issue_key": "RHAIRFE-1"}
        request = {"kwargs": {"source_issues": {"RHAIRFE-1": {"fields": {"summary": "A summary", "description": "Original description"}}}}}
        write_json(input_dir / "task.json", task)
        write_json(input_dir / "request.json", request)
        prompt = workspace / ".adlc-eval/prompt.txt"
        prompt.parent.mkdir(parents=True)
        prompt.write_text("prompt", encoding="utf-8")
        artifact = workspace / "artifacts/rhai-feature-tasks/RHAIRFE-1.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("## Business Need\nA summary\nOriginal description\n\n## Strategy (AI Generated by Agentic SDLC Pipeline)\n\n### TL;DR\n\n### Technical Approach\n\n### Acceptance Criteria\n\n## Staff Engineer / SME Input\n", encoding="utf-8")
        write_json(workspace / ".adlc-eval/container-result.json", {"status": "succeeded", "prompt_sha256": sha256_file(prompt)})
        (workspace / ".adlc-eval/events.jsonl").write_text(json.dumps({"type": "claude.stream", "event": {"type": "result", "session_id": "s", "modelUsage": {"model-a": {"inputTokens": 1}}}}) + "\n", encoding="utf-8")
        result = check_trial(self.root, trial, {"accepted_resolved_models": ["model-a"]})
        self.assertTrue(result["eligible"])

    def test_judge_reduction_and_reporting_keep_inconclusive_distinct(self) -> None:
        first = {"status": "succeeded", "judgment": {"preference": "X"}}
        second = {"status": "succeeded", "judgment": {"preference": "X"}}
        self.assertEqual("inconclusive", _reduce(first, second, {"X": "A", "Y": "B"}, {"X": "B", "Y": "A"})["outcome"])
        with self.assertRaises(EvalError):
            _validate_judgment({}, ("one",))

    def test_judge_output_accepts_one_json_fence_only(self) -> None:
        self.assertEqual({"preference": "X"}, parse_judgment_json("```json\n{\"preference\": \"X\"}\n```"))
        self.assertEqual({"preference": "Y"}, parse_judgment_json("```json\n{\"preference\": \"Y\"}"))
        with self.assertRaises(ValueError):
            parse_judgment_json("Here is the result:\n```json\n{}\n```")


if __name__ == "__main__":
    unittest.main()
