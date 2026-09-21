"""Normalize terminal Claude stream evidence without inventing missing values."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _events(path: Path) -> list[dict[str, Any]]:
    values = []
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        event = raw.get("event") if raw.get("type") == "claude.stream" else raw
        if isinstance(event, dict):
            values.append(event)
    return values


def usage(path: Path) -> dict[str, Any]:
    """Read the final terminal event for each session; never double count it."""
    terminals: dict[str, dict[str, Any]] = {}
    for event in _events(path):
        if event.get("type") != "result":
            continue
        session = event.get("session_id")
        if not isinstance(session, str):
            session = f"unkeyed-{len(terminals)}"
        terminals[session] = event
    totals: dict[str, Any] = {
        "available": bool(terminals), "sessions": len(terminals), "models": {},
        "cost_usd": None, "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        "thinking_tokens": 0,
    }
    total_cost = 0.0
    seen_cost = False
    for event in terminals.values():
        models = event.get("modelUsage")
        if not isinstance(models, dict):
            continue
        for model, raw in models.items():
            if not isinstance(model, str) or not isinstance(raw, dict):
                continue
            target = totals["models"].setdefault(model, {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
                                                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
                                                          "thinking_tokens": 0})
            mapping = (("costUSD", "cost_usd"), ("inputTokens", "input_tokens"),
                       ("outputTokens", "output_tokens"), ("cacheReadInputTokens", "cache_read_input_tokens"),
                       ("cacheCreationInputTokens", "cache_creation_input_tokens"), ("thinkingTokens", "thinking_tokens"))
            for source, destination in mapping:
                value = raw.get(source)
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                target[destination] += value
                if destination == "cost_usd":
                    total_cost += float(value)
                    seen_cost = True
                else:
                    totals[destination] += value
    if seen_cost:
        totals["cost_usd"] = round(total_cost, 8)
    return totals


def observed_models(path: Path) -> list[str]:
    return sorted(usage(path).get("models", {}))


def prohibited_activity(path: Path) -> list[str]:
    """Extract actual tool calls that violate the bounded worker contract."""
    violations = []
    for event in _events(path):
        if event.get("type") != "assistant":
            continue
        contents = event.get("message", {}).get("content", [])
        if not isinstance(contents, list):
            continue
        for block in contents:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            name = block.get("name")
            inputs = block.get("input", {})
            command = inputs.get("command", "") if isinstance(inputs, dict) else ""
            if name in {"Task", "Agent"}:
                violations.append(str(name))
            if name == "Bash":
                shell = str(command)
                # The plugin itself lives in an ``adlc-workflow`` directory.
                # Match only a direct workflow command (bare or under scripts/),
                # never that harmless parent-directory component. The bounded
                # refine worker is expected to use helpers such as
                # adlc-template-path and adlc-artifact-path.
                workflow_command = re.search(
                    r"(?:^|/)scripts/adlc-workflow(?=[\"']|\s|$)|(?<![\w/-])adlc-workflow(?=[\"']|\s|$)", shell
                )
                forbidden_helper = re.search(r"(?<![\w-])(adlc-submit|adlc-task|adlc-jira-issue)(?![\w-])", shell)
                network_call = re.search(r"(?<![\w-])curl\s", shell)
                if workflow_command or forbidden_helper or network_call:
                    violations.append(shell[:240])
    return violations


def tool_uses(path: Path) -> list[str]:
    values = []
    for event in _events(path):
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                values.append(str(block.get("name", "unknown")))
    return values
