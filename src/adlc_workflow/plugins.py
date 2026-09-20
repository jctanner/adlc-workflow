"""Reviewed concern-package registry and activation contract."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml


class PluginError(ValueError):
    """Raised when a concern package is unavailable or unsafe to activate."""


ALLOWED_POLICIES = {"required", "advisory"}
ALLOWED_CAPABILITIES = {
    "refine_context",
    "refine_task",
    "review_task",
    "projection",
}
RESOURCE_KEYS = {"context", "task", "review", "output_schema", "schema"}


def _safe_relative_path(package_root: Path, value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise PluginError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise PluginError(f"{field} must stay inside the plugin package")
    resolved = (package_root / path).resolve()
    if not resolved.is_relative_to(package_root.resolve()):
        raise PluginError(f"{field} escapes the plugin package")
    if not resolved.is_file():
        raise PluginError(f"{field} does not exist: {value}")
    return resolved


def _resource_paths(package_root: Path, value: Any, field: str) -> list[Path]:
    if isinstance(value, str):
        return [_safe_relative_path(package_root, value, field)]
    if isinstance(value, list):
        return [_safe_relative_path(package_root, item, f"{field}[{index}]") for index, item in enumerate(value)]
    raise PluginError(f"{field} must be a path or list of paths")


def _validate_activation(value: Any) -> dict[str, Any]:
    if value in (None, {}):
        return {}
    if not isinstance(value, dict):
        raise PluginError("activation must be a mapping")
    for key in value:
        if key not in {"any", "all", "requires"}:
            raise PluginError(f"unsupported activation key: {key}")
        if not isinstance(value[key], list):
            raise PluginError(f"activation.{key} must be a list")
    return value


def load_manifest(package_root: str | Path) -> dict[str, Any]:
    """Load and validate one concern package manifest and shipped resources."""
    package_root = Path(package_root).resolve()
    path = package_root / "manifest.yaml"
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise PluginError(f"cannot load plugin manifest: {path}") from exc
    if not isinstance(manifest, dict):
        raise PluginError("plugin manifest must be a mapping")
    if manifest.get("api_version") != "adlc.plugin/v1":
        raise PluginError(f"unsupported plugin api_version: {manifest.get('api_version')!r}")
    plugin_id = manifest.get("id")
    version = manifest.get("version")
    policy = manifest.get("policy", "advisory")
    if not isinstance(plugin_id, str) or not plugin_id or "/" in plugin_id or ".." in plugin_id:
        raise PluginError("plugin manifest requires a safe id")
    if not isinstance(version, str) or not version:
        raise PluginError(f"plugin {plugin_id} requires a version")
    if policy not in ALLOWED_POLICIES:
        raise PluginError(f"plugin {plugin_id} has unsupported policy: {policy}")
    capabilities = manifest.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(value, str) for value in capabilities):
        raise PluginError(f"plugin {plugin_id} capabilities must be a list of strings")
    unknown = set(capabilities) - ALLOWED_CAPABILITIES
    if unknown:
        raise PluginError(f"plugin {plugin_id} requests unsupported capabilities: {sorted(unknown)}")
    activation = _validate_activation(manifest.get("activation", {}))

    resources: list[Path] = []
    contributions = manifest.get("contributions", {})
    if contributions and not isinstance(contributions, dict):
        raise PluginError(f"plugin {plugin_id} contributions must be a mapping")
    if isinstance(contributions, dict):
        for section, values in contributions.items():
            if section not in {"before_refine", "after_refine", "review", "projection"}:
                raise PluginError(f"plugin {plugin_id} has unsupported contribution: {section}")
            if not isinstance(values, dict):
                raise PluginError(f"plugin {plugin_id} contribution {section} must be a mapping")
            for key, value in values.items():
                if key in RESOURCE_KEYS:
                    resources.extend(_resource_paths(package_root, value, f"contributions.{section}.{key}"))

    digest = hashlib.sha256()
    for resource in [path, *sorted(resources)]:
        digest.update(resource.relative_to(package_root).as_posix().encode())
        digest.update(b"\0")
        digest.update(resource.read_bytes())
    return {
        "id": plugin_id,
        "version": version,
        "policy": policy,
        "capabilities": list(capabilities),
        "activation": activation,
        "manifest_path": str(path),
        "digest": f"sha256:{digest.hexdigest()}",
        "_root": package_root,
    }


def _field_value(document: dict[str, Any], reference: str) -> Any:
    value: Any = document
    for part in reference.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _condition(document: dict[str, Any], condition: Any) -> bool:
    if not isinstance(condition, dict) or len(condition) != 1:
        raise PluginError("activation conditions must contain one operator")
    operator, operands = next(iter(condition.items()))
    if not isinstance(operands, list) or not operands:
        raise PluginError(f"activation.{operator} requires operands")
    reference = operands[0]
    if not isinstance(reference, str):
        raise PluginError(f"activation.{operator} reference must be a string")
    actual = _field_value(document, reference)
    if operator == "exists":
        return (actual is not None) is bool(operands[1] if len(operands) > 1 else True)
    if len(operands) != 2:
        raise PluginError(f"activation.{operator} requires exactly two operands")
    expected = operands[1]
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "in":
        return actual in expected if isinstance(expected, list) else False
    raise PluginError(f"unsupported activation operator: {operator}")


def _is_active(manifest: dict[str, Any], capture: dict[str, Any]) -> bool:
    activation = manifest["activation"]
    if not activation:
        return True
    if "requires" in activation and not all(_field_value(capture, value) is not None for value in activation["requires"]):
        return False
    any_rules = activation.get("any", [])
    all_rules = activation.get("all", [])
    return (not any_rules or any(_condition(capture, rule) for rule in any_rules)) and (
        not all_rules or all(_condition(capture, rule) for rule in all_rules)
    )


def activate_plugins(
    install_root: str | Path,
    profile: dict[str, Any],
    capture: dict[str, Any],
) -> list[dict[str, Any]]:
    """Load configured concerns and return deterministic activation receipts."""
    install_root = Path(install_root).resolve()
    concerns = profile.get("concerns", [])
    if not isinstance(concerns, list):
        raise PluginError("profile concerns must be a list")
    receipts = []
    for concern in concerns:
        requested = {"id": concern} if isinstance(concern, str) else concern
        if not isinstance(requested, dict) or not isinstance(requested.get("id"), str):
            raise PluginError("each profile concern requires an id")
        package_root = install_root / "plugins" / requested["id"]
        manifest = load_manifest(package_root)
        if requested.get("version") not in (None, manifest["version"]):
            raise PluginError(f"plugin {manifest['id']} version does not match the profile")
        if requested.get("digest") not in (None, manifest["digest"]):
            raise PluginError(f"plugin {manifest['id']} digest does not match the profile")
        active = _is_active(manifest, capture)
        receipts.append({
            "id": manifest["id"],
            "version": manifest["version"],
            "policy": manifest["policy"],
            "capabilities": manifest["capabilities"],
            "digest": manifest["digest"],
            "active": active,
            "reason": "activation matched" if active else "activation did not match",
        })
        if manifest["policy"] == "required" and not active:
            raise PluginError(f"required plugin is not applicable: {manifest['id']}")
    return receipts
