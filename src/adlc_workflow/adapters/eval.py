"""Fixture and recorded-response adapters for evaluation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .git import LocalArchivePublisher


class EvalPublisher:
    def publish(self, workspace_root: Path, run_id: str, items: list[dict[str, Any]], profile: dict[str, Any]) -> dict[str, Any]:
        receipt = LocalArchivePublisher().publish(workspace_root, run_id, items, profile, mode="eval")
        receipt["adapter"] = "eval-archive"
        return receipt
