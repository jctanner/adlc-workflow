"""Opt-in end-to-end test for the Jira emulator and Claude plugin runtime."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EMULATOR = ROOT.parent / "checkouts" / "jctanner" / "jira-emulator"
ADC = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=10) as response:
        value = json.loads(response.read())
    assert isinstance(value, dict)
    return value


def _vertex_environment() -> dict[str, str]:
    """Read only the non-secret Vertex settings from ~/bin/claude.vertex."""
    values = {}
    launcher = Path.home() / "bin" / "claude.vertex"
    if launcher.is_file():
        pattern = re.compile(r"^\s*export\s+(CLAUDE_CODE_USE_VERTEX|ANTHROPIC_VERTEX_PROJECT_ID|CLOUD_ML_REGION)=(.*)$")
        for line in launcher.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line)
            if match:
                values[match.group(1)] = match.group(2).strip().strip("\"'")
    return {name: os.environ.get(name, value) for name, value in values.items()}


@pytest.mark.integration
def test_claude_plugin_against_jira_emulator(tmp_path: Path) -> None:
    if os.getenv("ADLC_RUN_LIVE_AGENT") != "1":
        pytest.skip("set ADLC_RUN_LIVE_AGENT=1 to run the live Vertex integration")
    for executable in ("podman", "uv"):
        if shutil.which(executable) is None:
            pytest.skip(f"{executable} is required")
    if not EMULATOR.is_dir():
        pytest.skip(f"local jira-emulator checkout not found: {EMULATOR}")
    if not ADC.is_file():
        pytest.skip(f"Google ADC file not found: {ADC}")

    image = os.getenv("ADLC_AGENT_IMAGE", "adlc-claude-task-runner:local")
    if subprocess.run(["podman", "image", "exists", image], check=False).returncode != 0:
        pytest.skip(f"Podman image not found: {image}; build adlc-workflow/Dockerfile.claude first")

    port = _port()
    database = tmp_path / "jira.db"
    emulator = subprocess.Popen(
        ["uv", "run", "--project", str(EMULATOR), "python", "-m", "jira_emulator", "serve"],
        cwd=EMULATOR,
        env={**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{database}", "HOST": "127.0.0.1", "PORT": str(port), "SEED_DATA": "true"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if emulator.poll() is not None:
                output = emulator.stderr.read() if emulator.stderr else ""
                pytest.fail(f"jira-emulator exited early: {output[-2000:]}")
            try:
                _json_request(f"{base_url}/rest/api/2/project")
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.25)
        else:
            pytest.fail("jira-emulator did not become ready")

        seeded = _json_request(f"{base_url}/rest/api/2/issue", method="POST", payload={"fields": {
            "project": {"key": "RHAIRFE"},
            "summary": "Integration-test feature",
            "description": "A feature used to verify the ADLC Claude plugin.",
            "issuetype": {"name": "Feature Request"},
            "labels": ["rfe-creator-rubric-pass", "strat-creator-3.6"],
            "customfield_10855": "3.6 GA RHOAI RELEASE",
        }})
        issue_key = seeded["key"]

        runtime = tmp_path / "workspace"
        shutil.copytree(ROOT, runtime)
        credentials_target = "/home/evaluator/.config/gcloud/application_default_credentials.json"
        vertex_vars = _vertex_environment()
        vertex_vars["GOOGLE_APPLICATION_CREDENTIALS"] = credentials_target
        command = (
            "set -eu; mkdir -p /home/evaluator/.claude/plugins; "
            "cp -a /tmp/adlc-workflow /home/evaluator/.claude/plugins/adlc-workflow; "
            "exec claude --dangerously-skip-permissions "
            "--plugin-dir /home/evaluator/.claude/plugins/adlc-workflow "
            f"--model ${{ADLC_CLAUDE_MODEL:-claude-haiku-4-5}} -p {json.dumps('/adlc-workflow:adlc-workflow ' + issue_key)}"
        )
        podman = ["podman", "run", "--rm", "--name", f"adlc-workflow-integration-{os.getpid()}",
                  "--userns=keep-id", "--workdir", "/workspace",
                  "--volume", f"{ROOT}:/tmp/adlc-workflow:ro",
                  "--volume", f"{runtime}:/workspace:rw",
                  "--volume", f"{ADC}:{credentials_target}:ro",
                  "--env", "CLAUDE_PLUGIN_ROOT=/home/evaluator/.claude/plugins/adlc-workflow",
                  "--env", f"ADLC_JIRA_URL={base_url}",
                  "--env", "ADLC_JIRA_TOKEN=jira-emulator-default-token"]
        for name, value in vertex_vars.items():
            podman.extend(["--env", f"{name}={value}"])
        podman.extend([image, "sh", "-lc", command])
        log_path = Path(os.getenv("ADLC_INTEGRATION_LOG", str(tmp_path / "claude-container.log")))
        log_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"ADLC integration log: {log_path}")
        with log_path.open("w", encoding="utf-8") as log:
            result = subprocess.run(
                podman,
                check=False,
                text=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=300,
            )
        if result.returncode != 0:
            log_tail = log_path.read_text(encoding="utf-8", errors="replace")[-8000:]
            pytest.fail(f"Claude container failed ({result.returncode}); log: {log_path}\n{log_tail}")

        assert (runtime / "artifacts" / "rhai-feature-selection.json").is_file()
        assert list((runtime / ".adlc" / "state" / "runs").glob("*/state.json"))
    finally:
        if emulator.poll() is None:
            emulator.terminate()
            try:
                emulator.wait(timeout=10)
            except subprocess.TimeoutExpired:
                emulator.kill()
