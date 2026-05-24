# Release Workflow Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the monolithic `vrg-release` into independent tools (`vrg-version`, `vrg-changelog`, `vrg-promote`), aligned with standard Git Flow mechanics.

**Architecture:** Independent library modules (`lib/version.py`, `lib/changelog.py`, `lib/promote.py`) expose pure logic — they generate files but never commit. Thin CLI entry points in `bin/` handle argument parsing. The branch owner (orchestrator or human via `vrg-commit`) owns the commits. The existing `vrg-release` orchestrator is refactored to call the independent tools and libraries. Migration steps 1–3 are independently releasable; step 4 is a big-bang cutover with vergil-actions.

**Tech Stack:** Python 3.12+, pytest, git-cliff, GitHub CLI (`gh`)

**Spec:** `docs/superpowers/specs/2026-05-24-release-workflow-decomposition-design.md`
**Issue:** #1069

---

## File Map

| File | Status | Responsibility |
|------|--------|----------------|
| `src/vergil_tooling/lib/version.py` | Modify | Add `part` parameter to `bump()` for minor/major |
| `src/vergil_tooling/bin/vrg_version.py` | Modify | Add `part` argument to `bump` subcommand |
| `tests/vergil_tooling/test_version.py` | Modify | Add tests for minor/major bumps |
| `src/vergil_tooling/lib/changelog.py` | Create | git-cliff wrapper: `generate_changelog()`, `generate_release_notes()` |
| `src/vergil_tooling/bin/vrg_changelog.py` | Create | CLI entry point for `vrg-changelog` |
| `tests/vergil_tooling/test_changelog.py` | Create | Tests for changelog library |
| `src/vergil_tooling/lib/promote.py` | Create | `promote()` — force-update rolling `vX.Y` tag |
| `src/vergil_tooling/bin/vrg_promote.py` | Create | CLI entry point for `vrg-promote` |
| `tests/vergil_tooling/test_promote.py` | Create | Tests for promote library |
| `pyproject.toml` | Modify | Add `vrg-changelog` and `vrg-promote` entry points |

The orchestrator refactor (Task 4) modifies the existing `release/` modules. That is a separate deliverable gated behind a big-bang cutover with vergil-actions and is outlined but not fully detailed in this plan — it will need its own implementation plan once Tasks 1–3 are complete and proven.

---

## Task 1: Extend `lib/version.py` with minor/major bump support

**Files:**
- Modify: `src/vergil_tooling/lib/version.py`
- Modify: `src/vergil_tooling/bin/vrg_version.py`
- Modify: `tests/vergil_tooling/test_version.py`

### Step 1.1: Write failing tests for minor bump

- [ ] Add tests to `tests/vergil_tooling/test_version.py`:

```python
def test_bump_minor_generic(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    result = bump(tmp_path, "minor")
    assert result == "1.3.0"
    assert (tmp_path / "VERSION").read_text().strip() == "1.3.0"


def test_bump_major_generic(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("1.2.3\n")
    result = bump(tmp_path, "major")
    assert result == "2.0.0"
    assert (tmp_path / "VERSION").read_text().strip() == "2.0.0"


def test_bump_minor_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("2.0.5\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "2.0.5"\n'
    )
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path, "minor")
    assert result == "2.1.0"
    assert (tmp_path / "VERSION").read_text().strip() == "2.1.0"
    text = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "2.1.0"' in text


def test_bump_major_python(tmp_path: Path) -> None:
    _write_toml(tmp_path, "python")
    (tmp_path / "VERSION").write_text("2.5.3\n")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "2.5.3"\n'
    )
    with patch("vergil_tooling.lib.version.subprocess.run"):
        result = bump(tmp_path, "major")
    assert result == "3.0.0"
    assert (tmp_path / "VERSION").read_text().strip() == "3.0.0"
    text = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "3.0.0"' in text


def test_bump_patch_default(tmp_path: Path) -> None:
    """Calling bump() with no part argument still increments patch."""
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    result = bump(tmp_path)
    assert result == "1.0.1"


def test_bump_invalid_part_raises(tmp_path: Path) -> None:
    _write_toml(tmp_path, "shell")
    (tmp_path / "VERSION").write_text("1.0.0\n")
    with pytest.raises(ValueError, match="part must be"):
        bump(tmp_path, "rc")
```

- [ ] Run tests to verify they fail:

```bash
vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -k "bump_minor or bump_major or bump_patch_default or bump_invalid_part" -v
```

Expected: FAIL — `bump()` does not accept a `part` argument.

### Step 1.2: Implement minor/major bump in `lib/version.py`

- [ ] Replace `_increment_patch` with a general `_increment_version` function and update `bump()` to accept a `part` parameter:

In `src/vergil_tooling/lib/version.py`, replace the `_increment_patch` function:

```python
def _increment_version(version: str, part: str) -> str:
    parts = version.split(".")
    if part == "patch":
        parts[2] = str(int(parts[2]) + 1)
    elif part == "minor":
        parts[1] = str(int(parts[1]) + 1)
        parts[2] = "0"
    elif part == "major":
        parts[0] = str(int(parts[0]) + 1)
        parts[1] = "0"
        parts[2] = "0"
    return ".".join(parts)
```

Update the `bump()` function signature and body:

```python
_VALID_PARTS = frozenset({"patch", "minor", "major"})


def bump(repo_root: Path, part: str = "patch") -> str:
    if part not in _VALID_PARTS:
        msg = f"part must be one of {sorted(_VALID_PARTS)}, got '{part}'"
        raise ValueError(msg)

    version_file = repo_root / VERSION_FILE
    if not version_file.is_file():
        msg = f"VERSION file not found at {repo_root}"
        raise FileNotFoundError(msg)
    old_version = version_file.read_text().strip()
    new_version = _increment_version(old_version, part)

    version_file.write_text(new_version + "\n")

    cfg = read_config(repo_root)
    language = cfg.project.primary_language
    if language in _LANGUAGES_WITH_SEPARATE_VERSION:
        try:
            lang_file = _discover_version_file(repo_root, language)
            if lang_file.is_file():
                _write_version(lang_file, language, old_version, new_version)
        except (FileNotFoundError, ValueError):
            pass

    _run_lockfile_maintenance(repo_root, language)
    return new_version
```

Remove the old `_increment_patch` function.

- [ ] Run tests to verify they pass:

```bash
vrg-container-run -- uv run pytest tests/vergil_tooling/test_version.py -v
```

Expected: ALL PASS (including all existing tests — `bump(repo_root)` still works because `part` defaults to `"patch"`).

### Step 1.3: Update CLI to accept bump part argument

- [ ] In `src/vergil_tooling/bin/vrg_version.py`, update the `bump` subcommand:

Replace the `sub.add_parser("bump", ...)` line and the `else` branch in `main()`:

```python
    bump_parser = sub.add_parser("bump", help="Increment version")
    bump_parser.add_argument(
        "part",
        nargs="?",
        choices=("patch", "minor", "major"),
        default="patch",
        help="Version component to bump (default: patch)",
    )
```

And update the else branch:

```python
    else:
        new_version = bump(repo_root, args.part)
        print(new_version)  # noqa: T201
```

- [ ] Run the full test suite to verify nothing is broken:

```bash
vrg-container-run -- uv run vrg-validate
```

Expected: PASS

### Step 1.4: Commit

- [ ] Commit with message:

```
feat(version): add minor and major bump support to vrg-version

Extend bump() with an optional part parameter accepting "patch"
(default), "minor", or "major". The CLI subcommand gains an
optional positional argument: vrg-version bump [minor|major].

Ref #1069
```

---

## Task 2: Extract `lib/changelog.py` and `vrg-changelog` CLI

**Files:**
- Create: `src/vergil_tooling/lib/changelog.py`
- Create: `src/vergil_tooling/bin/vrg_changelog.py`
- Create: `tests/vergil_tooling/test_changelog.py`
- Modify: `pyproject.toml` (add entry point)

### Step 2.1: Write failing tests for changelog library

- [ ] Create `tests/vergil_tooling/test_changelog.py`:

```python
"""Tests for vergil_tooling.lib.changelog."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import call, patch

import pytest

from vergil_tooling.lib.changelog import (
    RELEASE_NOTES_DIR,
    generate_changelog,
    generate_release_notes,
)


def test_generate_changelog_calls_git_cliff(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("old content\n")
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.return_value = __import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        generate_changelog(tmp_path, "2.0.34")
        args = mock_run.call_args[0][0]
        assert args[0] == "git-cliff"
        assert "--tag" in args
        assert "develop-v2.0.34" in args
        assert "-o" in args
        assert "CHANGELOG.md" in args[-1]


def test_generate_changelog_normalizes_trailing_newline(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("content\n\n\n")
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.return_value = __import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        generate_changelog(tmp_path, "1.0.0")
    assert changelog.read_text().endswith("\n")
    assert not changelog.read_text().endswith("\n\n")


def test_generate_release_notes_creates_dir_and_file(tmp_path: Path) -> None:
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.return_value = __import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        releases_dir = tmp_path / RELEASE_NOTES_DIR
        releases_dir.mkdir()
        output = tmp_path / RELEASE_NOTES_DIR / "v2.0.34.md"
        output.write_text("notes\n")
        result = generate_release_notes(tmp_path, "2.0.34")
        assert result == output
        args = mock_run.call_args[0][0]
        assert "--unreleased" in args


def test_generate_changelog_raises_on_cliff_failure(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("")
    with patch("vergil_tooling.lib.changelog.subprocess.run") as mock_run:
        mock_run.side_effect = __import__("subprocess").CalledProcessError(
            1, "git-cliff"
        )
        with pytest.raises(__import__("subprocess").CalledProcessError):
            generate_changelog(tmp_path, "1.0.0")
```

- [ ] Run tests to verify they fail:

```bash
vrg-container-run -- uv run pytest tests/vergil_tooling/test_changelog.py -v
```

Expected: FAIL — `vergil_tooling.lib.changelog` does not exist.

### Step 2.2: Implement `lib/changelog.py`

- [ ] Create `src/vergil_tooling/lib/changelog.py`:

```python
"""Changelog and release notes generation via git-cliff."""

from __future__ import annotations

import subprocess
import sys
from importlib.resources import files
from pathlib import Path

RELEASE_NOTES_DIR = "releases"


def generate_changelog(repo_root: Path, version: str) -> None:
    """Generate CHANGELOG.md using git-cliff."""
    tag = f"develop-v{version}"
    config_path = files("vergil_tooling.configs") / "cliff.toml"
    output = repo_root / "CHANGELOG.md"
    result = subprocess.run(  # noqa: S603
        (  # noqa: S607
            "git-cliff",
            "--config",
            str(config_path),
            "--tag",
            tag,
            "-o",
            str(output),
        ),
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    _normalize_trailing_newline(output)


def generate_release_notes(repo_root: Path, version: str) -> Path:
    """Generate per-release notes file using git-cliff."""
    tag = f"develop-v{version}"
    releases_dir = repo_root / RELEASE_NOTES_DIR
    releases_dir.mkdir(exist_ok=True)
    output = releases_dir / f"v{version}.md"
    config_path = files("vergil_tooling.configs") / "cliff-release-notes.toml"
    result = subprocess.run(  # noqa: S603
        (  # noqa: S607
            "git-cliff",
            "--config",
            str(config_path),
            "--tag",
            tag,
            "--unreleased",
            "-o",
            str(output),
        ),
        check=True,
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    _normalize_trailing_newline(output)
    return output


def _normalize_trailing_newline(path: Path) -> None:
    path.write_text(path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8")
```

- [ ] Run tests to verify they pass:

```bash
vrg-container-run -- uv run pytest tests/vergil_tooling/test_changelog.py -v
```

Expected: PASS

### Step 2.3: Create CLI entry point

- [ ] Create `src/vergil_tooling/bin/vrg_changelog.py`:

```python
"""CLI entry point for vrg-changelog."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vergil_tooling.lib.changelog import generate_changelog, generate_release_notes
from vergil_tooling.lib.version import show


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-changelog",
        description="Generate changelog and release notes via git-cliff",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--changelog-only",
        action="store_true",
        help="Generate only CHANGELOG.md",
    )
    group.add_argument(
        "--notes-only",
        action="store_true",
        help="Generate only releases/vX.Y.Z.md",
    )
    args = parser.parse_args()
    repo_root = Path.cwd()

    try:
        version = show(repo_root)
    except FileNotFoundError:
        print("Could not detect version — is there a VERSION file?", file=sys.stderr)
        return 1

    if args.notes_only:
        output = generate_release_notes(repo_root, version)
        print(f"Generated: {output}")
    elif args.changelog_only:
        generate_changelog(repo_root, version)
        print("Generated: CHANGELOG.md")
    else:
        generate_changelog(repo_root, version)
        output = generate_release_notes(repo_root, version)
        print(f"Generated: CHANGELOG.md, {output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] Add the entry point to `pyproject.toml` in the `[project.scripts]` section:

```toml
vrg-changelog = "vergil_tooling.bin.vrg_changelog:main"
```

### Step 2.4: Run full validation

- [ ] Run validation:

```bash
vrg-container-run -- uv run vrg-validate
```

Expected: PASS

### Step 2.5: Commit

- [ ] Commit with message:

```
feat(changelog): extract lib/changelog.py and vrg-changelog CLI

Move changelog and release notes generation from
release/prepare.py into a standalone library and CLI tool.
The library functions generate files without committing —
callers control the commit.

Ref #1069
```

---

## Task 3: Create `lib/promote.py` and `vrg-promote` CLI

**Files:**
- Create: `src/vergil_tooling/lib/promote.py`
- Create: `src/vergil_tooling/bin/vrg_promote.py`
- Create: `tests/vergil_tooling/test_promote.py`
- Modify: `pyproject.toml` (add entry point)

### Step 3.1: Write failing tests

- [ ] Create `tests/vergil_tooling/test_promote.py`:

```python
"""Tests for vergil_tooling.lib.promote."""

from __future__ import annotations

from unittest.mock import call, patch

import pytest

from vergil_tooling.lib.promote import promote


def test_promote_runs_tag_and_push() -> None:
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        mock_run.return_value = __import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        promote("2.0.34")
        assert mock_run.call_count == 2
        tag_call = mock_run.call_args_list[0]
        assert tag_call[0][0] == ["git", "tag", "-f", "v2.0", "v2.0.34"]
        push_call = mock_run.call_args_list[1]
        assert push_call[0][0] == ["git", "push", "origin", "v2.0", "--force"]


def test_promote_dry_run_does_not_execute() -> None:
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        promote("2.0.34", dry_run=True)
        mock_run.assert_not_called()


def test_promote_strips_v_prefix() -> None:
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        mock_run.return_value = __import__("subprocess").CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        promote("v2.0.34")
        tag_call = mock_run.call_args_list[0]
        assert tag_call[0][0] == ["git", "tag", "-f", "v2.0", "v2.0.34"]


def test_promote_raises_on_tag_failure() -> None:
    with patch("vergil_tooling.lib.promote.subprocess.run") as mock_run:
        mock_run.side_effect = __import__("subprocess").CalledProcessError(
            1, "git tag"
        )
        with pytest.raises(__import__("subprocess").CalledProcessError):
            promote("2.0.34")


def test_promote_invalid_version_raises() -> None:
    with pytest.raises(ValueError, match="not valid"):
        promote("invalid")
```

- [ ] Run tests to verify they fail:

```bash
vrg-container-run -- uv run pytest tests/vergil_tooling/test_promote.py -v
```

Expected: FAIL — `vergil_tooling.lib.promote` does not exist.

### Step 3.2: Implement `lib/promote.py`

- [ ] Create `src/vergil_tooling/lib/promote.py`:

```python
"""Rolling-tag management — force-update vX.Y to track vX.Y.Z."""

from __future__ import annotations

import re
import subprocess

_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$")


def promote(version: str, *, dry_run: bool = False) -> None:
    """Force-update the vX.Y rolling tag to point at vX.Y.Z."""
    m = _VERSION_RE.match(version)
    if not m:
        msg = f"'{version}' is not valid semver (expected X.Y.Z or vX.Y.Z)"
        raise ValueError(msg)

    bare = m.group(1)
    parts = bare.split(".")
    rolling_tag = f"v{parts[0]}.{parts[1]}"
    release_tag = f"v{bare}"

    if dry_run:
        print(f"Would force-update {rolling_tag} -> {release_tag}")
        print(f"Would push {rolling_tag} to origin")
        return

    print(f"Force-updating {rolling_tag} -> {release_tag}")
    subprocess.run(  # noqa: S603
        ["git", "tag", "-f", rolling_tag, release_tag],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )

    print(f"Pushing {rolling_tag} to origin")
    subprocess.run(  # noqa: S603
        ["git", "push", "origin", rolling_tag, "--force"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )

    print(f"Promoted: {rolling_tag} -> {release_tag}")
```

- [ ] Run tests to verify they pass:

```bash
vrg-container-run -- uv run pytest tests/vergil_tooling/test_promote.py -v
```

Expected: PASS

### Step 3.3: Create CLI entry point

- [ ] Create `src/vergil_tooling/bin/vrg_promote.py`:

```python
"""CLI entry point for vrg-promote."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib.promote import promote
from vergil_tooling.lib.version import show


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="vrg-promote",
        description="Force-update the vX.Y rolling tag to track vX.Y.Z",
    )
    parser.add_argument(
        "version",
        nargs="?",
        default=None,
        help="Version to promote (e.g., v2.0.34). Default: current version from VERSION file.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without executing.",
    )
    args = parser.parse_args()

    version = args.version
    if version is None:
        try:
            from pathlib import Path

            version = show(Path.cwd())
        except FileNotFoundError:
            print(
                "No version specified and no VERSION file found.",
                file=sys.stderr,
            )
            return 1

    try:
        promote(version, dry_run=args.dry_run)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] Add the entry point to `pyproject.toml` in the `[project.scripts]` section:

```toml
vrg-promote = "vergil_tooling.bin.vrg_promote:main"
```

### Step 3.4: Run full validation

- [ ] Run validation:

```bash
vrg-container-run -- uv run vrg-validate
```

Expected: PASS

### Step 3.5: Commit

- [ ] Commit with message:

```
feat(promote): add lib/promote.py and vrg-promote CLI

Standalone tool to force-update the vX.Y rolling tag to point at
a specific vX.Y.Z release tag. Uses subprocess.run(["git", ...])
directly — this is a human tool requiring push credentials.

Ref #1069
```

---

## Task 4: Orchestrator Refactor (Outline)

This task is the big-bang cutover. It is outlined here for planning
but will need its own detailed implementation plan once Tasks 1–3
are complete and proven. The cutover must be coordinated with
vergil-actions changes.

### Scope

**vergil-tooling changes:**
- [ ] Add `--no-promote` flag to `vrg-release` CLI (`bin/vrg_release.py`)
- [ ] Update `ReleaseContext` with new fields: `cd_main_jobs`, `cd_develop_jobs`, `promote` flag, `develop_cd_run_id`/`develop_cd_run_url`
- [ ] Update `preflight.py`: remove `_apply_version_override` (version bump moves to prepare phase on the release branch). Store override argument in `ReleaseContext` for prepare to act on. Refactor `_detect_version` to use `lib/version.show()`.
- [ ] Simplify `prepare.py`: remove `-X ours` merge of `origin/main`. Call `lib/changelog.generate_changelog()` and `lib/changelog.generate_release_notes()` instead of inline git-cliff calls. If version override was requested, call `lib/version.bump()` on the release branch.
- [ ] Rewrite `confirm.py`: use known job expectations (`docs` + `release` on main) instead of `vergil.toml` flags. Keep artifact verification (tags, GitHub Release).
- [ ] Rewrite `bump.py`: replace CI-action polling with orchestrator-driven back-merge. Create `release/post-X.Y.Z` branch from main, call `lib/version.bump()`, create PR to develop, wait and merge.
- [ ] Add new phase: verify CD on develop (same mechanics as confirm, expected jobs: `docs` only).
- [ ] Add new phase: promote (call `lib/promote.promote()`, gated on `--no-promote`).
- [ ] Update `orchestrator.py` phase list to match new sequence.
- [ ] Update `finalize.py` summary to include new fields.
- [ ] Update all affected tests.

**vergil-actions changes (separate repo):**
- [ ] Remove `version-bump-pr` action invocation from `cd-release.yml`
- [ ] Remove inline rolling-tag force-update from `cd-release.yml`
- [ ] Verify `cd-release.yml` still creates tags and GitHub Releases correctly after removal

**Cutover sequence:**
1. Merge vergil-tooling changes (new vrg-release is ready but not yet exercised)
2. Merge vergil-actions changes (removes old bump-PR and rolling-tag logic)
3. Release vergil-tooling using the new workflow
4. Update all managed repos to new vergil-actions version

---

## Dependency Graph

```
Task 1 (vrg-version)   ─┐
Task 2 (vrg-changelog)  ─┼──▶ Task 4 (orchestrator refactor)
Task 3 (vrg-promote)    ─┘
```

Tasks 1–3 are independent of each other and can be implemented in
parallel. Task 4 depends on all three being complete and merged.
