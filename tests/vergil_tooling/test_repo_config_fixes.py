"""Tests for the managed-file fixers (epic vergil-project/.github#1064, Tasks 3a and 3b)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import dependabot, gitignore, repo_config, repo_config_fixes
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
    fix_dependabot_yml,
    fix_gitignore,
    fix_hook_guard_shim,
    fix_workflow_refs,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vergil_tooling.lib.github_config import DiffItem

SHIM = Path(".claude/hooks/guard.sh")
SETTINGS = Path(".claude/settings.json")
CLAUDE_MD = Path("CLAUDE.md")
GITIGNORE = Path(".gitignore")
DEPENDABOT = dependabot.DEPENDABOT_PATH
VERGIL_TOML = Path("vergil.toml")
CI_YML = Path(".github/workflows/ci.yml")
OPS_YML = Path(".github/workflows/ops.yml")

_VERGIL_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"
primary-language = "{lang}"

[project.co-authors]

[ci]
versions = ["3.12"]

[dependencies]
vergil = "{version}"
"""

_ACTIONS = "vergil-project/vergil-actions/.github/workflows"

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


def _write_toml(root: Path, lang: str = "python", version: str = "v2.1") -> None:
    _write(root, VERGIL_TOML, _VERGIL_TOML.format(lang=lang, version=version))


def _workflow(*pins: str, eol: str = "\n") -> str:
    """A workflow calling one vergil-actions workflow per pin, then a third-party action."""
    lines = ["jobs:"]
    for i, pin in enumerate(pins):
        lines += [f"  job{i}:", f"    uses: {_ACTIONS}/ci-{i}.yml@{pin}"]
    lines += [
        "  other:",
        "    runs-on: ubuntu-latest",
        "    steps:",
        "      - uses: actions/checkout@v4",
    ]
    return eol.join(lines) + eol


def _write_in_sync(root: Path) -> None:
    """Write every file a fixer manages, each already correct."""
    _write(root, SHIM, _managed_shim(), 0o755)
    _write(root, CLAUDE_MD, _doc(load_claude_md_template()))
    _write(root, SETTINGS, json.dumps(_settings_template()))
    _write_toml(root)
    _write(root, GITIGNORE, gitignore.render_block("python") + "\nlocal-scratch/\n")
    _write(root, DEPENDABOT, dependabot.render("python"))
    _write(root, CI_YML, _workflow("v2.1", "v2.1"))


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


# --- .gitignore ------------------------------------------------------------


def test_gitignore_block_replaced_user_lines_kept(tmp_path: Path) -> None:
    """A stale fence is rewritten and moved first; the repo's own lines are kept, in order."""
    _write_toml(tmp_path, lang="python")
    stale = gitignore.render_block(None)  # base-only: the python fragment is missing
    path = _write(tmp_path, GITIGNORE, f"# repo-local\nscratch/\n{stale}*.local\n", 0o600)
    items: list[DiffItem] = []
    repo_config._check_gitignore(tmp_path, items)
    assert {item.field for item in items} == {"local.gitignore"}

    assert fix_gitignore(tmp_path) is True
    expected = gitignore.render_block("python") + "# repo-local\nscratch/\n*.local\n"
    assert path.read_text(encoding="utf-8") == expected
    assert _mode(path) == 0o600
    after: list[DiffItem] = []
    repo_config._check_gitignore(tmp_path, after)
    assert after == []
    assert fix_gitignore(tmp_path) is False


def test_gitignore_missing_is_created_0644(tmp_path: Path) -> None:
    """With no vergil.toml the fence is base-only, and a missing file is created."""
    assert fix_gitignore(tmp_path) is True
    path = tmp_path / GITIGNORE
    assert path.read_text(encoding="utf-8") == gitignore.render_block(None)
    assert _mode(path) == 0o644


def test_gitignore_bootstrap_drops_loose_managed_lines(tmp_path: Path) -> None:
    """A file with no fence gets one; loose managed patterns go and local ones stay."""
    _write_toml(tmp_path, lang="python")
    path = _write(tmp_path, GITIGNORE, ".venv/\nmy-notes.txt\n")
    assert fix_gitignore(tmp_path) is True
    assert path.read_text(encoding="utf-8") == (
        gitignore.render_block("python") + "\nmy-notes.txt\n"
    )


def test_gitignore_unloadable_config_raises_cannot_fix(tmp_path: Path) -> None:
    """A vergil.toml that fails to load leaves the fence language unknown: nothing is written."""
    _write(tmp_path, VERGIL_TOML, "[project]\n")
    path = _write(tmp_path, GITIGNORE, "scratch/\n")
    with pytest.raises(CannotFix) as caught:
        fix_gitignore(tmp_path)
    assert caught.value.check == "local.gitignore"
    assert caught.value.reason.startswith("cannot resolve the fence language: ")
    assert path.read_text(encoding="utf-8") == "scratch/\n"


# --- dependabot.yml --------------------------------------------------------


def test_dependabot_rendered_exactly(tmp_path: Path) -> None:
    """A local addition is replaced by the exact canonical render, keeping the mode."""
    _write_toml(tmp_path, lang="go")
    path = _write(tmp_path, DEPENDABOT, dependabot.render("go") + "  # a local addition\n", 0o640)
    items: list[DiffItem] = []
    repo_config._check_dependabot_yml(tmp_path, items)
    [item] = items
    assert (item.field, item.actual) == ("local.dependabot", "differs from the canonical render")

    assert fix_dependabot_yml(tmp_path) is True
    assert path.read_bytes() == dependabot.render("go").encode("utf-8")
    assert _mode(path) == 0o640
    assert fix_dependabot_yml(tmp_path) is False


def test_dependabot_missing_is_created_0644(tmp_path: Path) -> None:
    _write_toml(tmp_path, lang="python")
    assert fix_dependabot_yml(tmp_path) is True
    path = tmp_path / DEPENDABOT
    assert path.read_text(encoding="utf-8") == dependabot.render("python")
    assert _mode(path) == 0o644


def test_dependabot_unloadable_config_raises_cannot_fix(tmp_path: Path) -> None:
    _write(tmp_path, VERGIL_TOML, "[project]\n")
    with pytest.raises(CannotFix) as caught:
        fix_dependabot_yml(tmp_path)
    assert caught.value.check == "local.dependabot"
    assert caught.value.reason.startswith("cannot resolve the dependabot language: ")
    assert not (tmp_path / DEPENDABOT).exists()


# --- workflow ref pins -----------------------------------------------------


def test_workflow_pins_rewritten_to_vergil_toml_version(tmp_path: Path) -> None:
    """Every drifting pin in every workflow is set to the version; nothing else changes."""
    _write_toml(tmp_path, version="v2.3")
    ci = _write(tmp_path, CI_YML, _workflow("v2.1", "v2.3"), 0o600)
    ops = _write(tmp_path, OPS_YML, _workflow("v2.0", eol="\r\n"))
    current = _write(tmp_path, Path(".github/workflows/docs.yaml"), _workflow("v2.3"))
    items: list[DiffItem] = []
    repo_config._check_workflow_refs(tmp_path, items)
    assert [(i.expected, i.actual) for i in items] == [
        ("ci.yml: v2.3", "ci.yml: v2.1"),
        ("ops.yml: v2.3", "ops.yml: v2.0"),
    ]
    mtime = current.stat().st_mtime_ns

    assert fix_workflow_refs(tmp_path) is True
    assert ci.read_bytes() == _workflow("v2.3", "v2.3").encode("utf-8")
    assert _mode(ci) == 0o600
    assert ops.read_bytes() == _workflow("v2.3", eol="\r\n").encode("utf-8")
    assert current.stat().st_mtime_ns == mtime
    assert b"actions/checkout@v4" in ci.read_bytes()
    after: list[DiffItem] = []
    repo_config._check_workflow_refs(tmp_path, after)
    assert after == []
    assert fix_workflow_refs(tmp_path) is False


@pytest.mark.parametrize("version", ['"2.3"', '"v2"', "2.3"])
def test_workflow_pins_not_set_to_a_malformed_version(tmp_path: Path, version: str) -> None:
    """A [dependencies].vergil that no pin can carry is drift with no fix: nothing is written."""
    _write(tmp_path, VERGIL_TOML, f"[dependencies]\nvergil = {version}\n")
    path = _write(tmp_path, CI_YML, _workflow("v2.1"))
    items: list[DiffItem] = []
    repo_config._check_workflow_refs(tmp_path, items)
    assert len(items) == 2
    assert items[-1].expected == "[dependencies].vergil as a vX.Y version"

    with pytest.raises(CannotFix) as caught:
        fix_workflow_refs(tmp_path)
    assert caught.value.check == "local.workflow_ref"
    assert "the pins cannot be set to it" in caught.value.reason
    assert path.read_bytes() == _workflow("v2.1").encode("utf-8")


def test_workflow_pins_without_vergil_toml_are_not_judged(tmp_path: Path) -> None:
    """A missing vergil.toml is the vergil.toml check's to report, so nothing changes."""
    path = _write(tmp_path, CI_YML, _workflow("v2.1"))
    assert fix_workflow_refs(tmp_path) is False
    assert path.read_bytes() == _workflow("v2.1").encode("utf-8")


def test_rewrite_workflow_refs_refuses_a_malformed_version() -> None:
    from vergil_tooling.lib.vergil_refs import rewrite_workflow_refs

    with pytest.raises(ValueError, match="not a vX.Y pin version: '2.3'"):
        rewrite_workflow_refs(_workflow("v2.1"), "2.3")


# --- every fixer -----------------------------------------------------------


def test_fixers_registry() -> None:
    assert [
        fix_hook_guard_shim,
        fix_claude_md,
        fix_claude_settings,
        fix_gitignore,
        fix_dependabot_yml,
        fix_workflow_refs,
    ] == FIXERS


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
    fix_gitignore: repo_config._check_gitignore,
    fix_dependabot_yml: repo_config._check_dependabot_yml,
    fix_workflow_refs: repo_config._check_workflow_refs,
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
    fix_gitignore: {
        "missing": lambda root: None,
        "in-sync-base-only": lambda root: _write(root, GITIGNORE, gitignore.render_block(None)),
        "in-sync": lambda root: (
            _write_toml(root, lang="python"),
            _write(root, GITIGNORE, gitignore.render_block("python") + "\nscratch/\n"),
        ),
        "in-sync-crlf": lambda root: (
            _write_toml(root, lang="cpp"),
            _write(root, GITIGNORE, gitignore.render_block("cpp").replace("\n", "\r\n")),
        ),
        "wrong-language": lambda root: (
            _write_toml(root, lang="go"),
            _write(root, GITIGNORE, gitignore.render_block("python")),
        ),
        "fence-not-first": lambda root: _write(
            root, GITIGNORE, "scratch/\n" + gitignore.render_block(None)
        ),
        "no-final-newline": lambda root: _write(
            root, GITIGNORE, gitignore.render_block(None) + "scratch/"
        ),
        "stray-managed-pattern": lambda root: _write(
            root, GITIGNORE, gitignore.render_block(None) + gitignore.load_base()[0] + "\n"
        ),
        "no-fence": lambda root: _write(root, GITIGNORE, ".venv/\nscratch/\n"),
        "unclosed-fence": lambda root: _write(
            root, GITIGNORE, gitignore.render_block(None).replace(gitignore.MANAGED_END, "")
        ),
        "unloadable-config": lambda root: _write(root, VERGIL_TOML, "[project]\n"),
    },
    fix_dependabot_yml: {
        "missing": lambda root: None,
        "in-sync-no-config": lambda root: _write(root, DEPENDABOT, dependabot.render(None)),
        "in-sync": lambda root: (
            _write_toml(root, lang="rust"),
            _write(root, DEPENDABOT, dependabot.render("rust")),
        ),
        "in-sync-unmapped-language": lambda root: (
            _write_toml(root, lang="cpp"),
            _write(root, DEPENDABOT, dependabot.render(None)),
        ),
        "wrong-language": lambda root: (
            _write_toml(root, lang="python"),
            _write(root, DEPENDABOT, dependabot.render("go")),
        ),
        "local-addition": lambda root: _write(
            root, DEPENDABOT, dependabot.render(None) + "# local\n"
        ),
        "empty": lambda root: _write(root, DEPENDABOT, ""),
        "unloadable-config": lambda root: _write(root, VERGIL_TOML, "[project]\n"),
    },
    fix_workflow_refs: {
        "no-vergil-toml": lambda root: _write(root, CI_YML, _workflow("v2.0")),
        "no-workflows": lambda root: _write_toml(root),
        "in-sync": lambda root: (
            _write_toml(root, version="v2.1"),
            _write(root, CI_YML, _workflow("v2.1", "v2.1")),
        ),
        "one-stale-pin": lambda root: (
            _write_toml(root, version="v2.1"),
            _write(root, CI_YML, _workflow("v2.1", "v2.0")),
        ),
        "stale-pins-in-two-files": lambda root: (
            _write_toml(root, version="v3.0"),
            _write(root, CI_YML, _workflow("v2.1")),
            _write(root, OPS_YML, _workflow("v2.9", eol="\r\n")),
        ),
        "newer-pin": lambda root: (
            _write_toml(root, version="v2.1"),
            _write(root, CI_YML, _workflow("v2.10")),
        ),
        "malformed-version": lambda root: (
            _write_toml(root, version="2.1"),
            _write(root, CI_YML, _workflow("v2.1")),
        ),
        "malformed-version-no-pins": lambda root: _write_toml(root, version="2.1"),
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
