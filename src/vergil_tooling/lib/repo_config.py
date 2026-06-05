"""Local repository configuration audit checks.

Checks files on disk — vergil.toml, CLAUDE.md, .claude/settings.json,
.claude/hooks/guard.sh — without making any API calls.
"""

from __future__ import annotations

import importlib.resources
import json
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.config import ConfigError, read_config
from vergil_tooling.lib.github_config import ConfigDiff, DiffItem

if TYPE_CHECKING:
    from pathlib import Path


def _load_template() -> str:
    return (
        importlib.resources.files("vergil_tooling.data")
        .joinpath("claude_md_consumer.md")
        .read_text(encoding="utf-8")
    )


def _load_settings_template() -> dict[str, Any]:
    raw = (
        importlib.resources.files("vergil_tooling.data")
        .joinpath("claude_settings.json")
        .read_text(encoding="utf-8")
    )
    template = json.loads(raw)
    if not isinstance(template, dict):  # pragma: no cover - packaging error
        msg = "claude_settings.json template is not a JSON object"
        raise TypeError(msg)
    return template


def audit_local_config(repo_root: Path) -> ConfigDiff:
    """Run all local config checks against a repo root directory."""
    items: list[DiffItem] = []
    _check_vergil_toml(repo_root, items)
    _check_hook_guard_shim(repo_root, items)
    _check_claude_md(repo_root, items)
    _check_claude_settings(repo_root, items)
    return ConfigDiff(items=items)


def _check_vergil_toml(repo_root: Path, items: list[DiffItem]) -> None:
    toml_path = repo_root / "vergil.toml"
    if not toml_path.is_file():
        items.append(
            DiffItem(
                field="local.vergil_toml",
                expected="present",
                actual="missing",
            )
        )
        return
    try:
        read_config(repo_root)
    except (ConfigError, FileNotFoundError) as exc:
        items.append(
            DiffItem(
                field="local.vergil_toml",
                expected="valid",
                actual=str(exc),
            )
        )


def _check_claude_md(repo_root: Path, items: list[DiffItem]) -> None:
    claude_md = repo_root / "CLAUDE.md"
    if not claude_md.is_file():
        items.append(
            DiffItem(
                field="local.claude_md",
                expected="present",
                actual="missing",
            )
        )
        return

    content = claude_md.read_text(encoding="utf-8")
    template = _load_template()
    if template not in content:
        items.append(
            DiffItem(
                field="local.claude_md",
                expected="template present",
                actual="template not found",
            )
        )


def _check_hook_guard_shim(repo_root: Path, items: list[DiffItem]) -> None:
    shim_path = repo_root / ".claude" / "hooks" / "guard.sh"
    if not shim_path.is_file():
        items.append(
            DiffItem(
                field="local.hook_guard_shim",
                expected="present",
                actual="missing",
            )
        )


def _check_claude_settings(repo_root: Path, items: list[DiffItem]) -> None:
    settings_path = repo_root / ".claude" / "settings.json"
    if not settings_path.is_file():
        items.append(
            DiffItem(
                field="local.claude_settings",
                expected="present",
                actual="missing",
            )
        )
        return

    try:
        raw = json.loads(settings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        items.append(
            DiffItem(
                field="local.claude_settings",
                expected="valid JSON",
                actual=str(exc),
            )
        )
        return

    if not isinstance(raw, dict):
        items.append(
            DiffItem(
                field="local.claude_settings",
                expected="JSON object",
                actual=type(raw).__name__,
            )
        )
        return

    template = _load_settings_template()
    _check_settings_section(
        raw,
        template,
        key="extraKnownMarketplaces",
        field="local.claude_settings.marketplace",
        items=items,
    )
    _check_settings_section(
        raw,
        template,
        key="enabledPlugins",
        field="local.claude_settings.plugin",
        items=items,
    )


def _check_settings_section(
    raw: dict[str, Any],
    template: dict[str, Any],
    *,
    key: str,
    field: str,
    items: list[DiffItem],
) -> None:
    """Require every template entry under ``key`` to match exactly.

    Same pattern as the CLAUDE.md template-presence check: the
    canonical entries must be present and equal; repos may add extra
    entries of their own. Catches plugin-manager clobbering of
    ``enabledPlugins`` / ``extraKnownMarketplaces`` (issue #1427).
    """
    expected_entries = template.get(key, {})
    actual_section = raw.get(key, {})
    if not isinstance(actual_section, dict):
        items.append(
            DiffItem(
                field=field,
                expected=f"{key} object",
                actual=f"{key} is not an object",
            )
        )
        return

    for name, expected_value in expected_entries.items():
        actual_value = actual_section.get(name)
        if actual_value != expected_value:
            items.append(
                DiffItem(
                    field=field,
                    expected=f"{name} = {json.dumps(expected_value, sort_keys=True)}",
                    actual=(
                        "missing"
                        if name not in actual_section
                        else f"{name} = {json.dumps(actual_value, sort_keys=True)}"
                    ),
                )
            )
