"""Mode, trust, adapter, and effect admission for workflow requests/bundles."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


class AdmissionError(ValueError):
    """Raised when a request or bundle is not permitted for its mode."""


MODES = {"production", "local", "eval"}
ADAPTER_EFFECTS = {"jira": "jira-write", "archive": "archive-write"}
FORBIDDEN_REQUEST_KEYS = {
    "adapters", "api_key", "credentials", "jira_token", "password", "secret", "token",
}


def load_policy(path: str | Path) -> dict[str, Any]:
    """Load and minimally validate the reviewed launch-mode policy."""
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise AdmissionError(f"cannot load admission policy: {path}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("modes"), dict):
        raise AdmissionError("admission policy requires a modes mapping")
    missing = MODES - set(value["modes"])
    if missing:
        raise AdmissionError(f"admission policy is missing modes: {sorted(missing)}")
    return value


def _mode_policy(policy: dict[str, Any], mode: str) -> dict[str, Any]:
    modes = policy.get("modes")
    config = modes.get(mode) if isinstance(modes, dict) else None
    if not isinstance(config, dict):
        raise AdmissionError(f"mode {mode!r} is not configured")
    return config


def _reject_secrets(kwargs: dict[str, Any]) -> None:
    supplied = sorted(FORBIDDEN_REQUEST_KEYS.intersection(kwargs))
    if supplied:
        raise AdmissionError(f"request cannot carry credentials or adapters: {supplied}")


def admit_request(request: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Return a non-sensitive admission receipt for a workflow request."""
    if not isinstance(request, dict) or not isinstance(request.get("kwargs"), dict):
        raise AdmissionError("request must contain a kwargs object")
    kwargs = request["kwargs"]
    mode = kwargs.get("mode")
    if mode not in MODES:
        raise AdmissionError("request mode must be production, local, or eval")
    config = _mode_policy(policy, mode)
    _reject_secrets(kwargs)
    trusted = kwargs.get("trusted", False)
    if not isinstance(trusted, bool):
        raise AdmissionError("request trusted flag must be boolean")
    if config.get("trusted", False) and not trusted:
        raise AdmissionError(f"mode {mode} requires a trusted request")
    if mode != "production" and kwargs.get("production", False):
        raise AdmissionError(f"mode {mode} cannot request production effects")

    configured_effects = config.get("effect_classes", [])
    if not isinstance(configured_effects, list) or not all(isinstance(value, str) for value in configured_effects):
        raise AdmissionError(f"mode {mode} has invalid effect_classes policy")
    requested_effects = kwargs.get("effect_classes", configured_effects)
    if not isinstance(requested_effects, list) or not all(isinstance(value, str) for value in requested_effects):
        raise AdmissionError("effect_classes must be a list of strings")
    if not set(requested_effects).issubset(configured_effects):
        raise AdmissionError(f"request effect_classes exceed the {mode} policy")
    required_effects = config.get("required_effect_classes", [])
    if not set(required_effects).issubset(requested_effects):
        raise AdmissionError(f"request is missing required {mode} effect classes")

    adapters = config.get("adapters", [])
    if not isinstance(adapters, list) or not all(isinstance(value, str) for value in adapters):
        raise AdmissionError(f"mode {mode} has invalid adapters policy")
    return {
        "schema_version": 1,
        "mode": mode,
        "trusted": trusted,
        "adapters": list(adapters),
        "effect_classes": list(requested_effects),
    }


def admit_bundle(manifest: dict[str, Any], expected_mode: str, policy: dict[str, Any]) -> None:
    """Reject a bundle whose mode/trust metadata does not match admission."""
    if expected_mode not in MODES:
        raise AdmissionError(f"unknown expected bundle mode: {expected_mode}")
    if not isinstance(manifest, dict) or manifest.get("mode") != expected_mode:
        raise AdmissionError("bundle mode does not match the admitted request")
    if expected_mode == "production" and manifest.get("trusted") is not True:
        raise AdmissionError("production bundle must carry trusted=true")
    _mode_policy(policy, expected_mode)


def enforce_effect_permissions(admission: dict[str, Any], adapters: list[str]) -> None:
    """Ensure configured publication effects are covered by admission."""
    mode = admission.get("mode")
    if mode not in MODES:
        raise AdmissionError("publication has no valid admission mode")
    if mode == "production" and admission.get("trusted") is not True:
        raise AdmissionError("production publication requires trusted admission")
    if not isinstance(admission.get("effect_classes"), list):
        raise AdmissionError("admission has no effect permission list")
    if not isinstance(admission.get("adapters"), list):
        raise AdmissionError("admission has no adapter permission list")
    unknown = set(adapters) - set(ADAPTER_EFFECTS)
    if unknown:
        raise AdmissionError(f"publication uses unknown adapters: {sorted(unknown)}")
    if not set(adapters).issubset(admission["adapters"]):
        raise AdmissionError(f"publication adapters exceed {mode} admission")
    effects = {ADAPTER_EFFECTS[adapter] for adapter in adapters}
    if not effects.issubset(admission["effect_classes"]):
        raise AdmissionError(f"publication effects exceed {mode} admission")


def enforce_profile_permissions(profile: dict[str, Any], admission: dict[str, Any]) -> None:
    """Check the selected profile's publication adapters before any effects."""
    workflow = profile.get("workflow")
    stages = workflow.get("stages") if isinstance(workflow, dict) else None
    publication = next(
        (stage for stage in stages or [] if isinstance(stage, dict) and stage.get("kind") == "publication"),
        None,
    )
    if not isinstance(publication, dict):
        raise AdmissionError("profile requires a publication stage for admission")
    adapters_by_mode = publication.get("adapters")
    if not isinstance(adapters_by_mode, dict):
        raise AdmissionError("publication stage requires adapters by mode")
    adapters = adapters_by_mode.get(admission.get("mode"), [])
    if not isinstance(adapters, list) or not all(isinstance(value, str) for value in adapters):
        raise AdmissionError("publication adapters must be a list of strings")
    enforce_effect_permissions(admission, adapters)
