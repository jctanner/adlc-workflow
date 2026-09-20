"""Private, file-backed workflow state with atomic writes and locking."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class StateError(RuntimeError):
    """Raised for missing or malformed private state."""


class StateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def run_dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id

    def state_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "state.json"

    def request_path(self, run_id: str) -> Path:
        return self.run_dir(run_id) / "request.json"

    def work_dir(self, run_id: str, work_id: str) -> Path:
        return self.run_dir(run_id) / "items" / work_id

    def task_path(self, run_id: str, work_id: str, task_id: str) -> Path:
        return self.work_dir(run_id, work_id) / "tasks" / f"{task_id}.json"

    def result_path(self, run_id: str, work_id: str, task_id: str) -> Path:
        return self.work_dir(run_id, work_id) / "results" / f"{task_id}.json"

    @staticmethod
    def _write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._write(path, json.dumps(value, indent=2, sort_keys=True) + "\n")

    def read_json(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StateError(f"missing state file: {path}") from exc
        except json.JSONDecodeError as exc:
            raise StateError(f"invalid JSON state file: {path}") from exc
        if not isinstance(value, dict):
            raise StateError(f"state file must contain an object: {path}")
        return value

    @contextmanager
    def lock(self, run_id: str) -> Iterator[None]:
        path = self.run_dir(run_id) / ".lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
