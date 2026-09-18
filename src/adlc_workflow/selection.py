"""Typed request normalization and frozen explicit-key selection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class SelectionError(ValueError):
    """Raised when a request cannot be normalized safely."""


KEY_RE = re.compile(r"^[A-Z][A-Z0-9]+-[0-9]+$")
SELECTORS = ("jql", "jql_default", "config")


@dataclass(frozen=True)
class Request:
    args: tuple[str, ...]
    operation: str
    mode: str
    batch_size: int | None
    batch_offset: int
    selector: str
    identity: str
    profile: str


def _keys(values: list[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if not isinstance(value, str) or not KEY_RE.fullmatch(value):
            raise SelectionError(f"invalid issue key: {value!r}")
        if value not in result:
            result.append(value)
    return tuple(result)


def normalize_request(document: dict[str, Any], *, default_profile: str) -> Request:
    if not isinstance(document, dict):
        raise SelectionError("request must be a JSON object")
    args = document.get("args", [])
    kwargs = document.get("kwargs", {})
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise SelectionError("request.args must be an array and request.kwargs must be an object")
    normalized_args = _keys(args)
    operation = kwargs.get("operation", "create")
    mode = kwargs.get("mode")
    identity = kwargs.get("identity", "local-developer")
    profile = kwargs.get("profile", default_profile)
    if operation not in {"create", "reprocess"}:
        raise SelectionError("operation must be create or reprocess")
    if mode not in {"production", "local", "eval"}:
        raise SelectionError("mode must be production, local, or eval")
    if not isinstance(identity, str) or not identity.strip():
        raise SelectionError("identity must be a non-empty string")
    if not isinstance(profile, str) or not profile.strip():
        raise SelectionError("profile must be a non-empty string")
    selected = [name for name in SELECTORS if kwargs.get(name) not in (None, False)]
    if normalized_args and selected:
        raise SelectionError("issue keys cannot be combined with jql, jql_default, or config")
    if len(selected) > 1:
        raise SelectionError("exactly one selection source is allowed")
    selector = "keys" if normalized_args else (selected[0] if selected else "empty")
    if selector == "jql" and not isinstance(kwargs["jql"], str):
        raise SelectionError("jql must be a string")
    if selector == "config" and not isinstance(kwargs["config"], str):
        raise SelectionError("config must be a path string")
    batch_size = kwargs.get("batch_size")
    if batch_size is not None and (not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0):
        raise SelectionError("batch_size must be a positive integer")
    batch_offset = kwargs.get("batch_offset", 0)
    if not isinstance(batch_offset, int) or isinstance(batch_offset, bool) or batch_offset < 0:
        raise SelectionError("batch_offset must be a non-negative integer")
    return Request(
        args=normalized_args,
        operation=operation,
        mode=mode,
        batch_size=batch_size,
        batch_offset=batch_offset,
        selector=selector,
        identity=identity,
        profile=profile,
    )


def freeze_selection(request: Request) -> dict[str, Any]:
    """Freeze explicit membership; external selectors are deferred adapters."""
    keys = list(request.args)
    if request.batch_offset:
        keys = keys[request.batch_offset :]
    if request.batch_size is not None:
        keys = keys[: request.batch_size]
    return {
        "schema_version": 1,
        "selector": request.selector,
        "operation": request.operation,
        "ordered_keys": keys,
        "duplicates_removed": len(request.args) - len(set(request.args)),
        "selection_status": "empty" if not keys else "frozen",
        "exclusion_reasons": {},
        "source": {"args": list(request.args)},
    }
