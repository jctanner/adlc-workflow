"""Verify that native reviewer executions overlap in a Claude JSONL trace."""

from typing import Any


def verify_parallel_reviewers(events: list[dict[str, Any]], agents: set[str]) -> dict[str, Any]:
    calls = {}
    started = {}
    completed = {}
    for index, event in enumerate(events):
        content = event.get("message", {}).get("content", [])
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "tool_use" and block.get("name") in {"Task", "Agent"}:
                name = block.get("input", {}).get("subagent_type")
                if name in agents:
                    calls[block["id"]] = name
        if event.get("type") != "system":
            continue
        if event.get("subtype") == "task_started":
            name = calls.get(event.get("tool_use_id"), event.get("subagent_type"))
            if name in agents:
                if name in [entry[0] for entry in started.values()]:
                    raise ValueError(f"reviewer launched more than once: {name}")
                started[event["task_id"]] = (name, index)
        elif event.get("subtype") == "task_notification" and event.get("task_id") in started:
            if event.get("status") != "completed":
                raise ValueError(f"reviewer did not succeed: {event['task_id']}")
            completed[event["task_id"]] = index
    if {entry[0] for entry in started.values()} != agents or set(started) != set(completed):
        raise ValueError("trace is missing reviewer starts or completions")
    # This demonstrates overlap, not merely multiple tool calls in a prompt.
    if max(entry[1] for entry in started.values()) >= min(completed.values()):
        raise ValueError("reviewers did not all start before the first completion")
    return {"status": "parallel", "reviewers": sorted(agents), "count": len(agents)}
