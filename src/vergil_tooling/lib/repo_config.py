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

    _check_marketplace(raw, items)
    _check_plugin_enabled(raw, items)


def _check_marketplace(raw: dict[str, Any], items: list[DiffItem]) -> None:
    marketplaces = raw.get("extraKnownMarketplaces", {})
    if not isinstance(marketplaces, dict):
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace",
                expected="vergil-marketplace configured",
                actual="extraKnownMarketplaces is not an object",
            )
        )
        return

    vergil_mp = marketplaces.get("vergil-marketplace")
    if not isinstance(vergil_mp, dict):
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace",
                expected="vergil-marketplace configured",
                actual="missing",
            )
        )
        return

    source = vergil_mp.get("source", {})
    if not isinstance(source, dict):
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace",
                expected="vergil-marketplace with source object",
                actual="source is not an object",
            )
        )
        return

    repo = source.get("repo", "")
    if repo != "vergil-project/vergil-claude-plugin":
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace_repo",
                expected="vergil-project/vergil-claude-plugin",
                actual=repo or "missing",
            )
        )


def _check_plugin_enabled(raw: dict[str, Any], items: list[DiffItem]) -> None:
    plugins = raw.get("enabledPlugins", {})
    if not isinstance(plugins, dict):
        items.append(
            DiffItem(
                field="local.claude_settings.plugin",
                expected="vergil@vergil-marketplace enabled",
                actual="enabledPlugins is not an object",
            )
        )
        return

    if not plugins.get("vergil@vergil-marketplace"):
        items.append(
            DiffItem(
                field="local.claude_settings.plugin",
                expected="vergil@vergil-marketplace enabled",
                actual="not enabled",
            )
        )
