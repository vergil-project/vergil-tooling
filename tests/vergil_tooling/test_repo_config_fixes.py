"""Tests for the managed-file fixers (epic vergil-project/.github#1064, Task 3a)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import repo_config, repo_config_fixes
from vergil_tooling.lib.repo_config import (
    CLAUDE_MD_MARKER_BEGIN,
    CLAUDE_MD_MARKER_END,
    load_claude_md_template,
    render_claude_settings,
    render_hook_guard_shim,
)
from vergil_tooling.lib.repo_config_fixes import (
    FIXERS,
    CannotFix,
    fix_claude_md,
    fix_claude_settings,
    fix_hook_guard_shim,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vergil_tooling.lib.github_config import DiffItem

SHIM = Path(".claude/hooks/guard.sh")
SETTINGS = Path(".claude/settings.json")
CLAUDE_MD = Path("CLAUDE.md")

HEAD = "# CLAUDE.md\r\n\r\nRepo-local intro.  \n\n"
TAIL = "\n## Project overview\n\nRepo-local tail, no final newline"


def _managed_shim() -> bytes:
    return render_hook_guard_shim().encode("utf-8")


def _settings_template() -> dict[str, object]:
    data = json.loads(render_claude_settings())
    assert isinstance(data, dict)
    return data


def _doc(region: str, head: str = HEAD, tail: str = TAIL) -> str:
    return f"{head}{CLAUDE_MD_MARKER_BEGIN}\n{region}{CLAUDE_MD_MARKER_END}\n{tail}"


def _write(root: Path, rel: Path, data: str | bytes, mode: int = 0o644) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode("utf-8") if isinstance(data, str) else data)
    path.chmod(mode)
    return path


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _write_in_sync(root: Path) -> None:
    """Write every file a Task 3a fixer manages, each already correct."""
    _write(root, SHIM, _managed_shim(), 0o755)
    _write(root, CLAUDE_MD, _doc(load_claude_md_template()))
    _write(root, SETTINGS, json.dumps(_settings_template()))


# --- the hook guard shim ---------------------------------------------------


def test_shim_not_executable_is_drift_and_fix_sets_0755(tmp_path: Path) -> None:
    """Correct bytes without the execute bits are drift, fixed to mode 0755 (review focus 5)."""
    path = _write(tmp_path, SHIM, _managed_shim(), 0o644)
    items: list[DiffItem] = []
    repo_config._check_hook_guard_shim(tmp_path, items)
    [item] = items
    assert (item.field, item.expected, item.actual) == (
        "local.hook_guard_shim",
        "executable (mode 0755)",
        "not executable (mode 0644)",
    )

    assert fix_hook_guard_shim(tmp_path) is True
    assert _mode(path) == 0o755
    assert path.read_bytes() == _managed_shim()
    assert fix_hook_guard_shim(tmp_path) is False


def test_shim_partly_executable_is_drift(tmp_path: Path) -> None:
    """Every execute bit is required: owner-only execute is still drift."""
    path = _write(tmp_path, SHIM, _managed_shim(), 0o744)
    assert fix_hook_guard_shim(tmp_path) is True
    assert _mode(path) == 0o755


def test_shim_missing_is_created_0755(tmp_path: Path) -> None:
    assert fix_hook_guard_shim(tmp_path) is True
    path = tmp_path / SHIM
    assert path.read_bytes() == _managed_shim()
    assert _mode(path) == 0o755


def test_shim_stale_copy_is_replaced(tmp_path: Path) -> None:
    path = _write(tmp_path, SHIM, _managed_shim().replace(b"\n", b"\r\n"), 0o755)
    assert fix_hook_guard_shim(tmp_path) is True
    assert path.read_bytes() == _managed_shim()


def test_shim_unreadable_raises_cannot_fix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable shim is reported by the check and never overwritten by the fixer."""
    path = _write(tmp_path, SHIM, b"local edit\n", 0o755)
    real_read_bytes = Path.read_bytes

    def failing_read_bytes(self: Path) -> bytes:
        if self == path:
            raise PermissionError("Permission denied")
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", failing_read_bytes)
    with pytest.raises(CannotFix) as caught:
        fix_hook_guard_shim(tmp_path)
    assert caught.value.check == "local.hook_guard_shim"
    assert caught.value.reason == "unreadable (Permission denied)"
    monkeypatch.undo()
    assert path.read_bytes() == b"local edit\n"


# --- CLAUDE.md -------------------------------------------------------------


def test_claude_md_region_replaced_outside_text_kept(tmp_path: Path) -> None:
    """Only the marked region changes: the head (CRLF included), tail and mode are kept."""
    stale = "## Memory\n\nAn outdated rule.\n"
    path = _write(tmp_path, CLAUDE_MD, _doc(stale), 0o600)

    assert fix_claude_md(tmp_path) is True
    assert path.read_bytes() == _doc(load_claude_md_template()).encode("utf-8")
    assert _mode(path) == 0o600
    items: list[DiffItem] = []
    repo_config._check_claude_md(tmp_path, items)
    assert items == []
    assert fix_claude_md(tmp_path) is False


def test_claude_md_blank_lines_by_markers_are_in_sync(tmp_path: Path) -> None:
    """Blank lines next to the markers are not drift, so the file is left untouched."""
    text = _doc(f"\n\n{load_claude_md_template()}\n")
    path = _write(tmp_path, CLAUDE_MD, text)
    assert fix_claude_md(tmp_path) is False
    assert path.read_bytes() == text.encode("utf-8")


def test_claude_md_template_without_final_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A template with no final newline still leaves the end marker on its own line."""
    monkeypatch.setattr(repo_config, "_load_template", lambda: "## Rules\n\nOne rule.")
    path = _write(tmp_path, CLAUDE_MD, _doc("## Rules\n\nOld rule.\n"))
    assert fix_claude_md(tmp_path) is True
    assert path.read_bytes() == _doc("## Rules\n\nOne rule.\n").encode("utf-8")
    assert fix_claude_md(tmp_path) is False


def test_failed_write_leaves_the_file_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed rename leaves the old file intact, no temp file behind, and raises."""
    path = _write(tmp_path, SHIM, b"stale\n", 0o755)

    def failing_replace(self: Path, target: Path) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(Path, "replace", failing_replace)
    with pytest.raises(OSError, match="disk full"):
        fix_hook_guard_shim(tmp_path)
    assert path.read_bytes() == b"stale\n"
    assert [p.name for p in path.parent.iterdir()] == ["guard.sh"]


def test_claude_md_without_markers_raises_cannot_fix(tmp_path: Path) -> None:
    path = _write(tmp_path, CLAUDE_MD, "# CLAUDE.md\n\nNo markers here.\n")
    items: list[DiffItem] = []
    repo_config._check_claude_md(tmp_path, items)
    [item] = items
    assert item.actual == "no template markers"

    with pytest.raises(CannotFix) as caught:
        fix_claude_md(tmp_path)
    assert (caught.value.check, caught.value.reason) == ("local.claude_md", "no template markers")
    assert str(caught.value) == "local.claude_md: cannot fix: no template markers"
    assert path.read_text(encoding="utf-8") == "# CLAUDE.md\n\nNo markers here.\n"


def test_claude_md_missing_raises_cannot_fix(tmp_path: Path) -> None:
    with pytest.raises(CannotFix, match="local.claude_md: cannot fix: missing"):
        fix_claude_md(tmp_path)
    assert not (tmp_path / CLAUDE_MD).exists()


# --- the Claude settings ---------------------------------------------------


def test_claude_settings_written_equal_to_template(tmp_path: Path) -> None:
    path = _write(tmp_path, SETTINGS, json.dumps({**_settings_template(), "env": {}}), 0o640)
    assert fix_claude_settings(tmp_path) is True
    assert path.read_text(encoding="utf-8") == render_claude_settings()
    assert json.loads(path.read_text(encoding="utf-8")) == _settings_template()
    assert _mode(path) == 0o640
    assert fix_claude_settings(tmp_path) is False


def test_claude_settings_missing_is_created_0644(tmp_path: Path) -> None:
    assert fix_claude_settings(tmp_path) is True
    path = tmp_path / SETTINGS
    assert path.read_text(encoding="utf-8") == render_claude_settings()
    assert _mode(path) == 0o644


@pytest.mark.parametrize("text", ["{not json", "[]"])
def test_claude_settings_invalid_is_replaced(tmp_path: Path, text: str) -> None:
    path = _write(tmp_path, SETTINGS, text)
    assert fix_claude_settings(tmp_path) is True
    assert path.read_text(encoding="utf-8") == render_claude_settings()


def test_claude_settings_reordered_is_in_sync(tmp_path: Path) -> None:
    """Key order and whitespace never count, so an equal-after-parsing file is untouched."""
    text = json.dumps(dict(reversed(list(_settings_template().items()))))
    path = _write(tmp_path, SETTINGS, text)
    assert fix_claude_settings(tmp_path) is False
    assert path.read_text(encoding="utf-8") == text


def test_claude_settings_unreadable_raises_cannot_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(tmp_path, SETTINGS, "{}")
    real_read_text = Path.read_text

    def failing_read_text(
        self: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if self == path:
            raise PermissionError("Permission denied")
        return real_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", failing_read_text)
    with pytest.raises(CannotFix, match="local.claude_settings: cannot fix: Permission denied"):
        fix_claude_settings(tmp_path)


def test_repo_init_renders_the_shared_settings() -> None:
    from vergil_tooling.lib.repo_init import render_claude_settings as init_render

    assert init_render() == render_claude_settings()


# --- every fixer -----------------------------------------------------------


def test_fixers_registry() -> None:
    assert [fix_hook_guard_shim, fix_claude_md, fix_claude_settings] == FIXERS


def test_in_sync_root_changes_nothing(tmp_path: Path) -> None:
    _write_in_sync(tmp_path)
    before = {p: (p.read_bytes(), _mode(p)) for p in tmp_path.rglob("*") if p.is_file()}
    assert [fixer(tmp_path) for fixer in FIXERS] == [False] * len(FIXERS)
    after = {p: (p.read_bytes(), _mode(p)) for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


#: The audit check each fixer shares its definition of "correct" with.
_CHECKS: dict[Callable[[Path], bool], Callable[[Path, list[DiffItem]], None]] = {
    fix_hook_guard_shim: repo_config._check_hook_guard_shim,
    fix_claude_md: repo_config._check_claude_md,
    fix_claude_settings: repo_config._check_claude_settings,
}

#: Fixture directories per fixer: each writes one state of the managed file.
_FIXTURES: dict[Callable[[Path], bool], dict[str, Callable[[Path], object]]] = {
    fix_hook_guard_shim: {
        "missing": lambda root: None,
        "in-sync": lambda root: _write(root, SHIM, _managed_shim(), 0o755),
        "in-sync-0775": lambda root: _write(root, SHIM, _managed_shim(), 0o775),
        "not-executable": lambda root: _write(root, SHIM, _managed_shim(), 0o644),
        "owner-executable": lambda root: _write(root, SHIM, _managed_shim(), 0o744),
        "stale": lambda root: _write(root, SHIM, b"#!/usr/bin/env bash\n", 0o755),
        "crlf": lambda root: _write(root, SHIM, _managed_shim().replace(b"\n", b"\r\n"), 0o755),
    },
    fix_claude_md: {
        "missing": lambda root: None,
        "in-sync": lambda root: _write(root, CLAUDE_MD, _doc(load_claude_md_template())),
        "in-sync-blank-padded": lambda root: _write(
            root, CLAUDE_MD, _doc(f"\n{load_claude_md_template()}\n\n")
        ),
        "stale-region": lambda root: _write(root, CLAUDE_MD, _doc("## Memory\n\nOld rule.\n")),
        "empty-region": lambda root: _write(root, CLAUDE_MD, _doc("")),
        "no-markers": lambda root: _write(root, CLAUDE_MD, "# CLAUDE.md\n"),
        "end-before-begin": lambda root: _write(
            root, CLAUDE_MD, f"{CLAUDE_MD_MARKER_END}\n{CLAUDE_MD_MARKER_BEGIN}\n"
        ),
        "two-begins": lambda root: _write(
            root,
            CLAUDE_MD,
            f"{CLAUDE_MD_MARKER_BEGIN}\n" + _doc(load_claude_md_template()),
        ),
    },
    fix_claude_settings: {
        "missing": lambda root: None,
        "in-sync": lambda root: _write(root, SETTINGS, render_claude_settings()),
        "in-sync-compact": lambda root: _write(root, SETTINGS, json.dumps(_settings_template())),
        "extra-key": lambda root: _write(
            root, SETTINGS, json.dumps({**_settings_template(), "env": {}})
        ),
        "empty-object": lambda root: _write(root, SETTINGS, "{}"),
        "invalid-json": lambda root: _write(root, SETTINGS, "{"),
        "not-an-object": lambda root: _write(root, SETTINGS, "[]"),
    },
}


def _cases() -> list[tuple[Callable[[Path], bool], str | None]]:
    """Every (fixer, fixture) pair; a fixer with no fixtures gets one failing case."""
    cases: list[tuple[Callable[[Path], bool], str | None]] = []
    for fixer in FIXERS:
        names = list(_FIXTURES.get(fixer, {})) or [None]
        cases.extend((fixer, name) for name in names)
    return cases


@pytest.mark.parametrize(
    ("fixer", "fixture"),
    _cases(),
    ids=lambda value: value if isinstance(value, str) else getattr(value, "__name__", "none"),
)
def test_check_and_fixer_agree(
    tmp_path: Path, fixer: Callable[[Path], bool], fixture: str | None
) -> None:
    """The check reports drift if and only if the fixer changes something or raises.

    Once the fixer has changed something, the check passes and a second run of
    the fixer changes nothing.
    """
    name = repr(fixer)
    assert fixture is not None, f"{name} has no fixtures in _FIXTURES"
    assert fixer in _CHECKS, f"{name} has no check in _CHECKS"
    check = _CHECKS[fixer]
    _FIXTURES[fixer][fixture](tmp_path)

    items: list[DiffItem] = []
    check(tmp_path, items)
    try:
        changed = fixer(tmp_path)
    except CannotFix:
        assert items, f"{name} raised CannotFix for {fixture}, but the check passed"
        return
    assert bool(items) == changed

    after: list[DiffItem] = []
    check(tmp_path, after)
    assert after == []
    assert fixer(tmp_path) is False


def test_cannot_fix_leaves_the_tree_untouched(tmp_path: Path) -> None:
    """A fixer that raises writes nothing, not even a parent directory."""
    with pytest.raises(CannotFix):
        repo_config_fixes.fix_claude_md(tmp_path)
    assert list(tmp_path.iterdir()) == []
