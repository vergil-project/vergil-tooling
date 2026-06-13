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
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.vergil_refs import (
    MARKETPLACE_NAME,
    expected_claude_ref,
    iter_workflow_refs,
    read_source_version,
)

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
    _check_workflow_refs(repo_root, items)
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


#: Markers delimiting the canonical-template region in a consuming
#: repo's CLAUDE.md. Exactly one begin/end pair; the lines between
#: them (ignoring leading/trailing blank lines) must equal the
#: template verbatim. Repo-local content outside the markers never
#: affects compliance. Issue #1439.
CLAUDE_MD_MARKER_BEGIN = "<!-- vergil:template:claude-md:begin -->"
CLAUDE_MD_MARKER_END = "<!-- vergil:template:claude-md:end -->"


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
    lines = content.splitlines()
    begins = [i for i, line in enumerate(lines) if line.strip() == CLAUDE_MD_MARKER_BEGIN]
    ends = [i for i, line in enumerate(lines) if line.strip() == CLAUDE_MD_MARKER_END]

    if not begins and not ends:
        # Legacy form (transition window): no markers, require the
        # template as a contiguous verbatim substring.
        if template not in content:
            items.append(
                DiffItem(
                    field="local.claude_md",
                    expected="template present",
                    actual="template not found",
                )
            )
        return

    malformed = _marker_structure_error(begins, ends)
    if malformed is not None:
        items.append(
            DiffItem(
                field="local.claude_md",
                expected="well-formed template markers",
                actual=malformed,
            )
        )
        return

    _compare_marked_region(lines, begins[0], ends[0], template, items)


def _marker_structure_error(begins: list[int], ends: list[int]) -> str | None:
    """Return a diagnostic for malformed markers, or None if well-formed."""
    if len(begins) > 1:
        return "multiple begin markers"
    if len(ends) > 1:
        return "multiple end markers"
    if not ends:
        return "begin marker without end marker"
    if not begins:
        return "end marker without begin marker"
    if ends[0] < begins[0]:
        return "end marker before begin marker"
    return None


def _compare_marked_region(
    lines: list[str],
    begin: int,
    end: int,
    template: str,
    items: list[DiffItem],
) -> None:
    """Compare the marked region against the template line by line.

    Reports the first divergent line with both the template line number
    and the absolute CLAUDE.md line number, so drift is fixable without
    manual diffing. Leading/trailing blank lines inside the region are
    tolerated.
    """
    start = begin + 1
    stop = end
    while start < stop and not lines[start].strip():
        start += 1
    while stop > start and not lines[stop - 1].strip():
        stop -= 1
    region = lines[start:stop]
    template_lines = template.splitlines()

    for offset in range(max(len(region), len(template_lines))):
        actual_line = region[offset] if offset < len(region) else None
        template_line = template_lines[offset] if offset < len(template_lines) else None
        if actual_line == template_line:
            continue
        expected = (
            f"template line {offset + 1}: {template_line}"
            if template_line is not None
            else "end of template"
        )
        actual = (
            f"CLAUDE.md line {start + offset + 1}: {actual_line}"
            if actual_line is not None
            else f"CLAUDE.md line {stop + 1}: end of marked region"
        )
        items.append(DiffItem(field="local.claude_md", expected=expected, actual=actual))
        return


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
    _check_marketplace_ref(repo_root, raw, template, items)
    _check_settings_section(
        raw,
        template,
        key="enabledPlugins",
        field="local.claude_settings.plugin",
        items=items,
    )


def _check_marketplace_ref(
    repo_root: Path,
    raw: dict[str, Any],
    template: dict[str, Any],
    items: list[DiffItem],
) -> None:
    """Assert the marketplace matches the template (except its version-derived
    ``source.ref``) and carries the expected ref.

    The ref is ``develop`` for the marketplace source repo, else the version
    from ``vergil.toml``. Everything else under the entry must equal the
    canonical template, so a wrong repo or source kind is still caught.
    """
    expected_source = (
        template.get("extraKnownMarketplaces", {})
        .get(MARKETPLACE_NAME, {})
        .get("source", {})
    )
    section = raw.get("extraKnownMarketplaces", {})
    entry = section.get(MARKETPLACE_NAME) if isinstance(section, dict) else None
    if not isinstance(entry, dict):
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace",
                expected=f"{MARKETPLACE_NAME} present",
                actual="missing",
            )
        )
        return
    source = entry.get("source")
    if not isinstance(source, dict):
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace",
                expected="source object",
                actual=f"source = {json.dumps(source, sort_keys=True)}",
            )
        )
        return
    source_without_ref = {k: v for k, v in source.items() if k != "ref"}
    if source_without_ref != expected_source:
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace",
                expected=f"source = {json.dumps(expected_source, sort_keys=True)}",
                actual=f"source = {json.dumps(source_without_ref, sort_keys=True)}",
            )
        )
        return
    try:
        expected_ref = expected_claude_ref(repo_root)
    except (UpdateDepsError, OSError, ValueError):
        return  # vergil.toml problems are reported by _check_vergil_toml
    actual_ref = source.get("ref")
    if actual_ref != expected_ref:
        items.append(
            DiffItem(
                field="local.claude_settings.marketplace_ref",
                expected=f"ref = {expected_ref}",
                actual=f"ref = {actual_ref}",
            )
        )


def _check_workflow_refs(repo_root: Path, items: list[DiffItem]) -> None:
    """Assert every vergil-* reusable-workflow pin matches the vergil.toml version.

    Unlike the marketplace ref, workflow pins use the version even for the
    marketplace source repo — the plugin repo still consumes vergil-actions at a
    pinned version.
    """
    try:
        expected = read_source_version(repo_root)
    except (UpdateDepsError, OSError, ValueError):
        return  # vergil.toml problems are reported by _check_vergil_toml
    for path, actual in iter_workflow_refs(repo_root):
        if actual != expected:
            items.append(
                DiffItem(
                    field="local.workflow_ref",
                    expected=f"{path.name}: {expected}",
                    actual=f"{path.name}: {actual}",
                )
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
