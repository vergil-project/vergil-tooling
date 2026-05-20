# vrg-release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement `vrg-release`, a fully mechanized, human-invoked CLI tool that automates the complete release workflow from develop to main.

**Architecture:** Functions-with-shared-context. Each phase is a standalone function in its own module under `lib/release/`. A thin orchestrator calls them sequentially, passing a `ReleaseContext` dataclass. Preflight runs outside the phase loop (no tracking issue yet). All git/gh operations use existing `lib/git.py` and `lib/github.py` wrappers.

**Tech Stack:** Python 3.12+, subprocess (git, gh, git-cliff), pytest with unittest.mock

**Design spec:** `docs/specs/2026-05-20-vrg-release-design.md`

---

## File Structure

### New files

| File | Responsibility |
|------|---------------|
| `src/vergil_tooling/bin/vrg_release.py` | CLI entry point: parse args, build context, call orchestrator |
| `src/vergil_tooling/lib/release/__init__.py` | Package init, re-exports `is_release_branch` |
| `src/vergil_tooling/lib/release/context.py` | `ReleaseContext` dataclass, `ReleaseError` exception |
| `src/vergil_tooling/lib/release/orchestrator.py` | Sequential phase runner |
| `src/vergil_tooling/lib/release/preflight.py` | Host prerequisite checks, config validation, version detection |
| `src/vergil_tooling/lib/release/tracking.py` | Issue creation, commenting, phase markers |
| `src/vergil_tooling/lib/release/prepare.py` | Branch creation, changelog generation, PR creation |
| `src/vergil_tooling/lib/release/merge.py` | Wait-poll-merge logic (shared by Phases 2 and 3) |
| `src/vergil_tooling/lib/release/bump.py` | Bump PR polling, linkage verification |
| `src/vergil_tooling/lib/release/confirm.py` | Workflow watching, artifact verification |
| `src/vergil_tooling/lib/release/finalize.py` | Close tracking issue, run vrg-finalize-repo |
| `src/vergil_tooling/lib/release/handoff.py` | Consumer-refresh display |
| `tests/vergil_tooling/test_release_context.py` | Tests for ReleaseContext and ReleaseError |
| `tests/vergil_tooling/test_release_tracking.py` | Tests for tracking module |
| `tests/vergil_tooling/test_release_preflight.py` | Tests for preflight checks |
| `tests/vergil_tooling/test_release_prepare.py` | Tests for prepare phase |
| `tests/vergil_tooling/test_release_merge.py` | Tests for merge logic |
| `tests/vergil_tooling/test_release_bump.py` | Tests for bump PR phase |
| `tests/vergil_tooling/test_release_confirm.py` | Tests for confirm phase |
| `tests/vergil_tooling/test_release_finalize.py` | Tests for finalize phase |
| `tests/vergil_tooling/test_release_handoff.py` | Tests for consumer-refresh |
| `tests/vergil_tooling/test_release_orchestrator.py` | Tests for orchestrator |
| `tests/vergil_tooling/test_vrg_release.py` | Tests for CLI entry point |

### Modified files

| File | Change |
|------|--------|
| `src/vergil_tooling/lib/config.py` | Add `consumer_refresh` and `docs_workflow` to `PublishConfig` |
| `src/vergil_tooling/lib/release.py` | Deleted — contents move to `lib/release/__init__.py` |
| `tests/vergil_tooling/test_release.py` | Update import path from `lib.release` to `lib.release` (package) |
| `tests/vergil_tooling/test_config.py` | Add tests for new PublishConfig fields |
| `pyproject.toml` | Add `vrg-release`, remove `vrg-prepare-release`, `vrg-merge-when-green`, `vrg-check-pr-merge` |

---

### Task 1: Extend PublishConfig with consumer_refresh and docs_workflow

**Files:**
- Modify: `src/vergil_tooling/lib/config.py:57-59` (PublishConfig), `:118-122` (parser)
- Modify: `tests/vergil_tooling/test_config.py`

- [ ] **Step 1: Write failing tests for new PublishConfig fields**

Add to `tests/vergil_tooling/test_config.py`:

```python
def test_publish_consumer_refresh(tmp_path: Path) -> None:
    toml = _VALID_TOML + '\n[publish]\nconsumer-refresh = "uv tool install pkg@v<VERSION>"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.publish.consumer_refresh == "uv tool install pkg@v<VERSION>"


def test_publish_consumer_refresh_default_none(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.publish.consumer_refresh is None


def test_publish_docs_workflow(tmp_path: Path) -> None:
    toml = _VALID_TOML + '\n[publish]\ndocs-workflow = "Pages"\n'
    (tmp_path / "vergil.toml").write_text(toml)
    cfg = read_config(tmp_path)
    assert cfg.publish.docs_workflow == "Pages"


def test_publish_docs_workflow_default(tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text(_VALID_TOML)
    cfg = read_config(tmp_path)
    assert cfg.publish.docs_workflow == "Documentation"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_config.py -v -k "consumer_refresh or docs_workflow"`
Expected: FAIL — `PublishConfig` has no `consumer_refresh` or `docs_workflow` attributes

- [ ] **Step 3: Extend PublishConfig dataclass**

In `src/vergil_tooling/lib/config.py`, update `PublishConfig`:

```python
@dataclass
class PublishConfig:
    release: bool
    docs: bool
    consumer_refresh: str | None
    docs_workflow: str
```

- [ ] **Step 4: Update the parser to populate new fields**

In `src/vergil_tooling/lib/config.py`, update the `publish` parsing block (around line 118):

```python
    publish_raw = raw.get("publish", {})
    publish = PublishConfig(
        release=bool(publish_raw.get("release", False)),
        docs=bool(publish_raw.get("docs", True)),
        consumer_refresh=publish_raw.get("consumer-refresh"),
        docs_workflow=str(publish_raw.get("docs-workflow", "Documentation")),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_config.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```
vrg-commit --type feat --scope config --message "add consumer_refresh and docs_workflow to PublishConfig" --body "Ref #919"
```

---

### Task 2: Migrate lib/release.py to lib/release/ package

**Files:**
- Delete: `src/vergil_tooling/lib/release.py`
- Create: `src/vergil_tooling/lib/release/__init__.py`
- Modify: `tests/vergil_tooling/test_release.py`

- [ ] **Step 1: Create the package with the existing function**

Create `src/vergil_tooling/lib/release/__init__.py` with the contents of the current `lib/release.py`:

```python
"""Release workflow utilities."""

from __future__ import annotations

import re

_LEGACY_RELEASE_RE = re.compile(
    r"^chore/(bump-version-|(\d+)-next-cycle-deps-)",
)


def is_release_branch(branch: str) -> bool:
    """Return True if *branch* belongs to the release workflow.

    Matches:
      - ``release/*`` — the primary release-branch prefix
      - ``chore/bump-version-*`` — auto-generated by the version-bump-pr action (legacy)
      - ``chore/<N>-next-cycle-deps-*`` — next-cycle dependency update branches (legacy)
    """
    return branch.startswith("release/") or bool(_LEGACY_RELEASE_RE.match(branch))
```

- [ ] **Step 2: Delete the old module**

Remove `src/vergil_tooling/lib/release.py`.

- [ ] **Step 3: Verify test imports still resolve**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release.py -v`
Expected: ALL PASS — the import `from vergil_tooling.lib.release import is_release_branch` resolves to the package's `__init__.py` identically.

- [ ] **Step 4: Run full validation**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type refactor --scope release --message "migrate lib/release.py to lib/release/ package" --body "Ref #919"
```

---

### Task 3: ReleaseContext and ReleaseError

**Files:**
- Create: `src/vergil_tooling/lib/release/context.py`
- Create: `tests/vergil_tooling/test_release_context.py`

- [ ] **Step 1: Write tests for ReleaseContext and ReleaseError**

Create `tests/vergil_tooling/test_release_context.py`:

```python
from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError


def test_context_required_fields() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    assert ctx.repo == "owner/repo"
    assert ctx.version == "2.1.0"
    assert ctx.repo_root == Path("/tmp/repo")
    assert ctx.version_override is None


def test_context_optional_fields_default_none() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    assert ctx.issue_number is None
    assert ctx.release_pr_url is None
    assert ctx.bump_pr_url is None
    assert ctx.tag is None


def test_context_fields_are_mutable() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    ctx.issue_number = 42
    ctx.release_pr_url = "https://github.com/owner/repo/pull/100"
    assert ctx.issue_number == 42
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"


def test_release_error_carries_diagnostics() -> None:
    err = ReleaseError(
        phase="merge-release",
        command="gh pr merge ...",
        message="CI check failed",
        detail="check 'lint' failed with status 'failure'",
    )
    assert err.phase == "merge-release"
    assert err.command == "gh pr merge ..."
    assert "CI check failed" in str(err)
    assert err.detail == "check 'lint' failed with status 'failure'"


def test_release_error_is_exception() -> None:
    with pytest.raises(ReleaseError, match="something broke"):
        raise ReleaseError(
            phase="prepare",
            command="git push",
            message="something broke",
        )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_context.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement context.py**

Create `src/vergil_tooling/lib/release/context.py`:

```python
"""Release workflow data types."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class ReleaseContext:
    """Shared state that flows through every release phase."""

    repo: str
    version: str
    repo_root: Path
    version_override: str | None

    issue_number: int | None = None
    issue_url: str | None = None
    release_branch: str | None = None
    release_pr_url: str | None = None

    release_merge_sha: str | None = None

    bump_pr_url: str | None = None
    next_version: str | None = None

    publish_run_id: str | None = None
    publish_run_url: str | None = None
    docs_run_id: str | None = None
    docs_run_url: str | None = None
    tag: str | None = None
    develop_tag: str | None = None
    release_url: str | None = None


class ReleaseError(Exception):
    """Raised when a release phase fails."""

    def __init__(
        self,
        phase: str,
        command: str,
        message: str,
        detail: str | None = None,
    ) -> None:
        self.phase = phase
        self.command = command
        self.detail = detail
        super().__init__(message)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_context.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add ReleaseContext dataclass and ReleaseError exception" --body "Ref #919"
```

---

### Task 4: Tracking Module

**Files:**
- Create: `src/vergil_tooling/lib/release/tracking.py`
- Create: `tests/vergil_tooling/test_release_tracking.py`

- [ ] **Step 1: Write tests for tracking functions**

Create `tests/vergil_tooling/test_release_tracking.py`:

```python
from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import call, patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import (
    comment_phase_complete,
    comment_phase_failed,
    create_tracking_issue,
    close_tracking_issue,
    find_existing_tracking_issue,
)

_MOD = "vergil_tooling.lib.release.tracking"


def _ctx() -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )


def test_create_tracking_issue() -> None:
    ctx = _ctx()
    with patch(_MOD + ".github.read_output", return_value="https://github.com/owner/repo/issues/42"):
        create_tracking_issue(ctx)
    assert ctx.issue_number == 42
    assert ctx.issue_url == "https://github.com/owner/repo/issues/42"


def test_create_tracking_issue_extracts_number_from_url() -> None:
    ctx = _ctx()
    with patch(_MOD + ".github.read_output", return_value="https://github.com/owner/repo/issues/999"):
        create_tracking_issue(ctx)
    assert ctx.issue_number == 999


def test_find_existing_tracking_issue_returns_url() -> None:
    with patch(
        _MOD + ".github.read_output",
        return_value="https://github.com/owner/repo/issues/10",
    ):
        result = find_existing_tracking_issue("owner/repo", "2.1.0")
    assert result == "https://github.com/owner/repo/issues/10"


def test_find_existing_tracking_issue_returns_none() -> None:
    with patch(_MOD + ".github.read_output", return_value=""):
        result = find_existing_tracking_issue("owner/repo", "2.1.0")
    assert result is None


def test_comment_phase_complete() -> None:
    ctx = _ctx()
    ctx.issue_number = 42
    ctx.release_pr_url = "https://github.com/owner/repo/pull/100"
    with patch(_MOD + ".github.run") as mock_run:
        comment_phase_complete(ctx, "prepare", "Branch: release/2.1.0\nPR: https://...")
        args = mock_run.call_args[0]
        body = " ".join(args)
        assert "vrg-release:prepare:complete" in body


def test_comment_phase_failed() -> None:
    ctx = _ctx()
    ctx.issue_number = 42
    exc = ReleaseError(
        phase="merge-release",
        command="gh pr merge ...",
        message="CI failed",
        detail="lint check failed",
    )
    with patch(_MOD + ".github.run") as mock_run:
        comment_phase_failed(ctx, "merge-release", exc)
        args = mock_run.call_args[0]
        body = " ".join(args)
        assert "vrg-release:merge-release:failed" in body


def test_close_tracking_issue() -> None:
    ctx = _ctx()
    ctx.issue_number = 42
    with patch(_MOD + ".github.run") as mock_run:
        close_tracking_issue(ctx, "Summary text here")
    assert mock_run.call_count == 2  # comment + close
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_tracking.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement tracking.py**

Create `src/vergil_tooling/lib/release/tracking.py`:

```python
"""GitHub tracking issue management for release operations."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import github

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError


def find_existing_tracking_issue(repo: str, version: str) -> str | None:
    """Return the URL of an open 'release: <version>' issue, or None."""
    result = github.read_output(
        "issue",
        "list",
        "--repo",
        repo,
        "--search",
        f"release: {version} in:title",
        "--state",
        "open",
        "--json",
        "url",
        "--jq",
        ".[0].url",
    )
    return result if result else None


def create_tracking_issue(ctx: ReleaseContext) -> None:
    """Create a release tracking issue and populate ctx."""
    body = f"## Release {ctx.version}\n\nRepo: {ctx.repo}\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False
    ) as f:
        f.write(body)
        tmp_path = f.name
    try:
        url = github.read_output(
            "issue",
            "create",
            "--repo",
            ctx.repo,
            "--title",
            f"release: {ctx.version}",
            "--body-file",
            tmp_path,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    match = re.search(r"/issues/(\d+)$", url)
    if not match:
        msg = f"Could not extract issue number from URL: {url}"
        raise ValueError(msg)
    ctx.issue_number = int(match.group(1))
    ctx.issue_url = url


def comment_phase_complete(
    ctx: ReleaseContext, phase: str, details: str
) -> None:
    """Post a phase-completion comment on the tracking issue."""
    body = f"<!-- vrg-release:{phase}:complete -->\n\n**{phase}** complete.\n\n{details}"
    _comment(ctx, body)


def comment_phase_failed(
    ctx: ReleaseContext, phase: str, exc: ReleaseError
) -> None:
    """Post a phase-failure comment on the tracking issue."""
    lines = [
        f"<!-- vrg-release:{phase}:failed -->",
        "",
        f"**{phase}** failed.",
        "",
        f"**Command:** `{exc.command}`",
        f"**Error:** {exc}",
    ]
    if exc.detail:
        lines.append(f"**Detail:** {exc.detail}")
    _comment(ctx, "\n".join(lines))


def close_tracking_issue(ctx: ReleaseContext, summary: str) -> None:
    """Post a summary comment and close the tracking issue."""
    _comment(ctx, summary)
    github.run(
        "issue",
        "close",
        str(ctx.issue_number),
        "--repo",
        ctx.repo,
    )


def _comment(ctx: ReleaseContext, body: str) -> None:
    """Post a comment on the tracking issue."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False
    ) as f:
        f.write(body)
        tmp_path = f.name
    try:
        github.run(
            "issue",
            "comment",
            str(ctx.issue_number),
            "--repo",
            ctx.repo,
            "--body-file",
            tmp_path,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_tracking.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add tracking module for release issue management" --body "Ref #919"
```

---

### Task 5: Preflight Module

This is the largest module — it absorbs version detection from `vrg-prepare-release` and adds all new preflight checks.

**Files:**
- Create: `src/vergil_tooling/lib/release/preflight.py`
- Create: `tests/vergil_tooling/test_release_preflight.py`

- [ ] **Step 1: Write tests for host prerequisite checks**

Create `tests/vergil_tooling/test_release_preflight.py`:

```python
from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.preflight import preflight

_MOD = "vergil_tooling.lib.release.preflight"


def _valid_toml() -> str:
    return (
        '[project]\n'
        'repository-type = "library"\n'
        'versioning-scheme = "semver"\n'
        'branching-model = "library-release"\n'
        'release-model = "tagged-release"\n'
        'primary-language = "python"\n'
        '[publish]\n'
        'release = true\n'
        'docs = true\n'
        '[ci]\n'
        'versions = ["3.12"]\n'
        '[dependencies]\n'
        'vergil = "v2.0"\n'
    )


@pytest.fixture()
def _repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "vergil.toml").write_text(_valid_toml())
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "2.1.0"\n'
    )
    return tmp_path


def test_preflight_fails_if_git_cliff_missing(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value=None),
        pytest.raises(ReleaseError, match="git-cliff"),
    ):
        preflight(
            version_override=None,
            repo_root=_repo,
        )


def test_preflight_fails_if_gh_auth_fails(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(
            _MOD + ".github.read_output",
            side_effect=Exception("not authenticated"),
        ),
        pytest.raises(ReleaseError, match="GitHub CLI"),
    ):
        preflight(
            version_override=None,
            repo_root=_repo,
        )


def test_preflight_fails_if_not_library_or_tooling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    toml = _valid_toml().replace(
        'repository-type = "library"',
        'repository-type = "documentation"',
    )
    (tmp_path / "vergil.toml").write_text(toml)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "test"\nversion = "1.0.0"\n'
    )
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="test-repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        pytest.raises(ReleaseError, match="repository_type"),
    ):
        preflight(version_override=None, repo_root=tmp_path)


def test_preflight_fails_if_version_matches_tag(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="test-repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(
            _MOD + ".git.read_output",
            return_value="v2.1.0",
        ),
        patch(_MOD + ".find_existing_tracking_issue", return_value=None),
        pytest.raises(ReleaseError, match="already tagged"),
    ):
        preflight(version_override=None, repo_root=_repo)


def test_preflight_returns_context(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="owner/repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(_MOD + ".git.read_output", return_value="v2.0.0"),
        patch(_MOD + ".find_existing_tracking_issue", return_value=None),
    ):
        ctx = preflight(version_override=None, repo_root=_repo)
    assert ctx.repo == "owner/repo"
    assert ctx.version == "2.1.0"
    assert ctx.repo_root == _repo


def test_preflight_fails_if_tracking_issue_exists(_repo: Path) -> None:
    with (
        patch("shutil.which", return_value="/usr/bin/git-cliff"),
        patch(_MOD + ".github.read_output", return_value="owner/repo"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(_MOD + ".git.read_output", return_value="v2.0.0"),
        patch(
            _MOD + ".find_existing_tracking_issue",
            return_value="https://github.com/owner/repo/issues/50",
        ),
        pytest.raises(ReleaseError, match="tracking issue already exists"),
    ):
        preflight(version_override=None, repo_root=_repo)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_preflight.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement preflight.py**

Create `src/vergil_tooling/lib/release/preflight.py`. This absorbs the version detection from `vrg_prepare_release.py` and adds all new preflight checks:

```python
"""Preflight checks for vrg-release."""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from vergil_tooling.lib import config, git, github
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import find_existing_tracking_issue

_VERSION_OVERRIDE_FIELDS = ("minor", "major")


def preflight(
    *,
    version_override: str | None,
    repo_root: Path,
) -> ReleaseContext:
    """Run all preflight checks and return an initialized ReleaseContext."""
    _check_host_prerequisites()
    repo = _check_gh_auth()
    cfg = _read_and_validate_config(repo_root)
    _check_branch_and_tree()
    _audit_repo_config(repo)
    version = _detect_version(repo_root)
    _check_version_not_tagged(version)
    _check_no_existing_tracking_issue(repo, version)

    if version_override in _VERSION_OVERRIDE_FIELDS:
        version = _apply_version_override(repo_root, version, version_override, cfg)

    print(f"Preflight passed: {repo} v{version}")
    return ReleaseContext(
        repo=repo,
        version=version,
        repo_root=repo_root,
        version_override=version_override,
    )


def _check_host_prerequisites() -> None:
    if shutil.which("git-cliff") is None:
        raise ReleaseError(
            phase="preflight",
            command="which git-cliff",
            message="git-cliff is not on PATH. Install it before running vrg-release.",
        )


def _check_gh_auth() -> str:
    try:
        return github.read_output(
            "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner",
        )
    except Exception as exc:
        raise ReleaseError(
            phase="preflight",
            command="gh repo view",
            message="GitHub CLI authentication failed.",
            detail=str(exc),
        ) from exc


def _read_and_validate_config(repo_root: Path) -> config.StConfig:
    cfg = config.read_config(repo_root)
    if cfg.project.repository_type not in ("library", "tooling"):
        raise ReleaseError(
            phase="preflight",
            command="read vergil.toml",
            message=(
                f"vrg-release requires repository_type 'library' or 'tooling', "
                f"got '{cfg.project.repository_type}'."
            ),
        )
    return cfg


def _check_branch_and_tree() -> None:
    branch = git.current_branch()
    if branch != "develop":
        raise ReleaseError(
            phase="preflight",
            command="git rev-parse --abbrev-ref HEAD",
            message=f"Must be on develop branch (currently on '{branch}').",
        )
    status = git.read_output("status", "--porcelain")
    if status:
        raise ReleaseError(
            phase="preflight",
            command="git status --porcelain",
            message="Working tree is not clean.",
            detail=status,
        )
    git.run("fetch", "--tags", "--force", "origin", "develop")
    local_sha = git.read_output("rev-parse", "HEAD")
    remote_sha = git.read_output("rev-parse", "origin/develop")
    if local_sha != remote_sha:
        raise ReleaseError(
            phase="preflight",
            command="git rev-parse HEAD vs origin/develop",
            message=(
                f"Local develop ({local_sha[:8]}) does not match "
                f"origin/develop ({remote_sha[:8]}). Pull latest first."
            ),
        )


def _audit_repo_config(repo: str) -> None:
    result = subprocess.run(
        ("vrg-github-repo-config", "audit", "--repo", repo),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ReleaseError(
            phase="preflight",
            command=f"vrg-github-repo-config audit --repo {repo}",
            message="Repository configuration is non-compliant.",
            detail=result.stdout + result.stderr,
        )


# -- version detection (absorbed from vrg-prepare-release) --


def _detect_python() -> str | None:
    path = Path("pyproject.toml")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _detect_maven() -> str | None:
    path = Path("pom.xml")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(
        r"<artifactId>[^<]+</artifactId>\s*<version>([^<]+)</version>", text
    )
    return match.group(1) if match else None


def _detect_go() -> str | None:
    if not Path("go.mod").is_file():
        return None
    for path in Path().rglob("version.go"):
        text = path.read_text(encoding="utf-8")
        match = re.search(r'(?:const\s+)?Version\s*=\s*"([^"]+)"', text)
        if match:
            return match.group(1)
    return None


def _detect_ruby() -> str | None:
    if not Path("Gemfile").is_file():
        return None
    for path in Path().rglob("version.rb"):
        text = path.read_text(encoding="utf-8")
        match = re.search(r"VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
        if match:
            return match.group(1)
    return None


def _detect_cargo() -> str | None:
    path = Path("Cargo.toml")
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return match.group(1) if match else None


def _detect_claude_plugin() -> str | None:
    import json

    path = Path(".claude-plugin/plugin.json")
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("version")


def _detect_version_file() -> str | None:
    path = Path("VERSION")
    if not path.is_file():
        return None
    version = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ReleaseError(
            phase="preflight",
            command="read VERSION",
            message=f"VERSION file contains '{version}' — not valid semver (MAJOR.MINOR.PATCH).",
        )
    return version


_DETECTORS = [
    _detect_python,
    _detect_maven,
    _detect_go,
    _detect_ruby,
    _detect_cargo,
    _detect_claude_plugin,
    _detect_version_file,
]


def _detect_version(repo_root: Path) -> str:
    import os

    prev = os.getcwd()
    os.chdir(repo_root)
    try:
        for detector in _DETECTORS:
            version = detector()
            if version is not None:
                return version
    finally:
        os.chdir(prev)
    raise ReleaseError(
        phase="preflight",
        command="detect version",
        message="Could not detect project version from any supported manifest.",
    )


def _check_version_not_tagged(version: str) -> None:
    latest_tag = git.read_output(
        "describe", "--tags", "--abbrev=0", "--match", "v*",
    )
    if latest_tag == f"v{version}":
        raise ReleaseError(
            phase="preflight",
            command=f"git describe --tags --match v*",
            message=(
                f"Version {version} is already tagged as {latest_tag}. "
                f"The post-publish version bump may not have run."
            ),
        )


def _check_no_existing_tracking_issue(repo: str, version: str) -> None:
    existing = find_existing_tracking_issue(repo, version)
    if existing is not None:
        raise ReleaseError(
            phase="preflight",
            command=f"gh issue list --search 'release: {version}'",
            message=(
                f"A tracking issue already exists for version {version}: {existing}\n"
                f"Close the stale issue or investigate before re-running."
            ),
        )


def _apply_version_override(
    repo_root: Path,
    current: str,
    override: str,
    cfg: config.StConfig,
) -> str:
    parts = current.split(".")
    if len(parts) != 3:
        raise ReleaseError(
            phase="preflight",
            command="version override",
            message=f"Version '{current}' is not valid semver for override.",
        )
    major, minor, _patch = int(parts[0]), int(parts[1]), int(parts[2])
    if override == "minor":
        target = f"{major}.{minor + 1}.0"
    else:
        target = f"{major + 1}.0.0"

    _bump_version_in_manifest(repo_root, current, target, cfg)
    print(f"Version override: {current} -> {target}")
    return target


def _bump_version_in_manifest(
    repo_root: Path, old: str, new: str, cfg: config.StConfig
) -> None:
    if cfg.project.primary_language == "python":
        path = repo_root / "pyproject.toml"
        text = path.read_text(encoding="utf-8")
        text = text.replace(f'version = "{old}"', f'version = "{new}"')
        path.write_text(text, encoding="utf-8")
        subprocess.run(("uv", "lock"), check=True, cwd=repo_root)
    else:
        raise ReleaseError(
            phase="preflight",
            command="version override",
            message=(
                f"Version override not yet implemented for "
                f"language '{cfg.project.primary_language}'."
            ),
        )
    git.run("add", "-A")
    git.run("commit", "-m", f"chore(release): bump version to {new}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_preflight.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full validation**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: PASS

- [ ] **Step 6: Commit**

```
vrg-commit --type feat --scope release --message "add preflight module with version detection and checks" --body "Ref #919"
```

---

### Task 6: Prepare Module

**Files:**
- Create: `src/vergil_tooling/lib/release/prepare.py`
- Create: `tests/vergil_tooling/test_release_prepare.py`

- [ ] **Step 1: Write tests for prepare phase**

Create `tests/vergil_tooling/test_release_prepare.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import call, patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.prepare import prepare

_MOD = "vergil_tooling.lib.release.prepare"


def _ctx(tmp_path: Path) -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=tmp_path,
        version_override=None,
    )


def test_prepare_creates_issue_branch_and_pr(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    git_calls: list[tuple[str, ...]] = []

    def mock_git_run(*args: str) -> None:
        git_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.run", side_effect=mock_git_run),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.read_output", return_value="abc1234"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

    assert ("checkout", "-b", "release/2.1.0") in git_calls
    assert ctx.release_branch == "release/2.1.0"
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"


def test_prepare_creates_tracking_issue_first(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    call_order: list[str] = []

    def track_issue(c: ReleaseContext) -> None:
        call_order.append("create_tracking_issue")
        c.issue_number = 42
        c.issue_url = "https://github.com/owner/repo/issues/42"

    def track_git(*args: str) -> None:
        call_order.append(f"git.run:{args[0]}")

    with (
        patch(_MOD + ".create_tracking_issue", side_effect=track_issue),
        patch(_MOD + ".git.run", side_effect=track_git),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.read_output", return_value="abc1234"),
        patch(_MOD + "._generate_changelog"),
        patch(_MOD + ".github.create_pr", return_value="https://github.com/owner/repo/pull/100"),
    ):
        prepare(ctx)
    assert call_order[0] == "create_tracking_issue"


def test_prepare_fails_if_branch_exists(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        pytest.raises(ReleaseError, match="already exists"),
    ):
        prepare(ctx)


def test_prepare_fails_if_no_changes(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(
            _MOD + "._generate_changelog",
            side_effect=ReleaseError(
                phase="prepare",
                command="git-cliff",
                message="No publishable changes.",
            ),
        ),
        pytest.raises(ReleaseError, match="publishable"),
    ):
        prepare(ctx)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_prepare.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement prepare.py**

Create `src/vergil_tooling/lib/release/prepare.py`:

```python
"""Phase 1: Prepare release — branch, changelog, PR."""

from __future__ import annotations

import subprocess
import tempfile
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import create_tracking_issue

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

RELEASE_NOTES_DIR = "releases"


def prepare(ctx: ReleaseContext) -> None:
    """Create tracking issue, release branch, changelog, and PR to main."""
    create_tracking_issue(ctx)
    print(f"Tracking issue created: {ctx.issue_url}")

    branch = f"release/{ctx.version}"

    if git.ref_exists(branch) or git.ref_exists(f"origin/{branch}"):
        raise ReleaseError(
            phase="prepare",
            command=f"git rev-parse {branch}",
            message=f"Release branch '{branch}' already exists.",
        )

    print(f"Creating branch: {branch}")
    git.run("checkout", "-b", branch)

    print("Merging main into release branch...")
    git.run("fetch", "--tags", "--force", "origin", "main")
    git.run(
        "merge",
        "origin/main",
        "-X",
        "ours",
        "-m",
        f"chore(release): merge main into {branch}",
    )

    _generate_changelog(ctx)

    print(f"Pushing branch: {branch}")
    git.run("push", "-u", "origin", branch)

    pr_url = _create_pr(ctx)

    git.run("checkout", "develop")

    ctx.release_branch = branch
    ctx.release_pr_url = pr_url
    print(f"Release PR created: {pr_url}")


def _generate_changelog(ctx: ReleaseContext) -> None:
    tag = f"develop-v{ctx.version}"
    print(f"Generating changelog with boundary tag: {tag}")
    config_path = files("vergil_tooling.configs") / "cliff.toml"
    subprocess.run(
        ("git-cliff", "--config", str(config_path), "--tag", tag, "-o", "CHANGELOG.md"),
        check=True,
    )
    _normalize_trailing_newline(Path("CHANGELOG.md"))
    git.run("add", "CHANGELOG.md")

    releases_dir = Path(RELEASE_NOTES_DIR)
    releases_dir.mkdir(exist_ok=True)
    output_file = releases_dir / f"v{ctx.version}.md"
    print(f"Generating release notes: {output_file}")
    release_notes_config = files("vergil_tooling.configs") / "cliff-release-notes.toml"
    subprocess.run(
        (
            "git-cliff",
            "--config",
            str(release_notes_config),
            "--tag",
            tag,
            "--unreleased",
            "-o",
            str(output_file),
        ),
        check=True,
    )
    _normalize_trailing_newline(output_file)
    git.run("add", str(releases_dir))

    status = git.read_output("status", "--porcelain")
    if not status:
        raise ReleaseError(
            phase="prepare",
            command="git-cliff",
            message=(
                f"No publishable changes since the last release. "
                f"All commits after develop-v{ctx.version} are filtered by git-cliff."
            ),
        )
    git.run("commit", "-m", f"chore(release): prepare {ctx.version}")


def _normalize_trailing_newline(path: Path) -> None:
    path.write_text(
        path.read_text(encoding="utf-8").rstrip() + "\n", encoding="utf-8"
    )


def _create_pr(ctx: ReleaseContext) -> str:
    title = f"release: {ctx.version}"
    body = (
        f"## Summary\n\nRelease {ctx.version}\n\n"
        f"Ref #{ctx.issue_number}\n\n"
        f"Generated with `vrg-release`\n"
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False
    ) as f:
        f.write(body)
        tmp_path = f.name
    try:
        return github.create_pr(base="main", title=title, body_file=tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_prepare.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add prepare module for branch creation and changelog" --body "Ref #919"
```

---

### Task 7: Merge Module

**Files:**
- Create: `src/vergil_tooling/lib/release/merge.py`
- Create: `tests/vergil_tooling/test_release_merge.py`

- [ ] **Step 1: Write tests for wait-poll-merge logic**

Create `tests/vergil_tooling/test_release_merge.py`:

```python
from __future__ import annotations

from unittest.mock import patch, call

import pytest

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import wait_and_merge

_MOD = "vergil_tooling.lib.release.merge"


def test_merge_succeeds_when_checks_pass() -> None:
    with (
        patch(_MOD + ".github.mergeable", return_value="MERGEABLE"),
        patch(_MOD + ".github.wait_for_checks"),
        patch(_MOD + ".github.merge_state_status", return_value="CLEAN"),
        patch(_MOD + ".github.merge") as mock_merge,
    ):
        sha = wait_and_merge("https://github.com/o/r/pull/1", phase="merge-release")
    mock_merge.assert_called_once_with("https://github.com/o/r/pull/1", strategy="merge")


def test_merge_fails_on_conflict() -> None:
    with (
        patch(_MOD + ".github.mergeable", return_value="CONFLICTING"),
        pytest.raises(ReleaseError, match="merge conflicts"),
    ):
        wait_and_merge("https://github.com/o/r/pull/1", phase="merge-release")


def test_merge_updates_branch_when_behind() -> None:
    states = iter(["BEHIND", "CLEAN"])
    with (
        patch(_MOD + ".github.mergeable", return_value="MERGEABLE"),
        patch(_MOD + ".github.wait_for_checks"),
        patch(_MOD + ".github.merge_state_status", side_effect=states),
        patch(_MOD + ".github.update_branch") as mock_update,
        patch(_MOD + ".github.merge"),
    ):
        wait_and_merge("https://github.com/o/r/pull/1", phase="merge-release")
    mock_update.assert_called_once()


def test_merge_gives_up_after_max_updates() -> None:
    with (
        patch(_MOD + ".github.mergeable", return_value="MERGEABLE"),
        patch(_MOD + ".github.wait_for_checks"),
        patch(_MOD + ".github.merge_state_status", return_value="BEHIND"),
        patch(_MOD + ".github.update_branch"),
        pytest.raises(ReleaseError, match="still behind"),
    ):
        wait_and_merge("https://github.com/o/r/pull/1", phase="merge-release")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_merge.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement merge.py**

Create `src/vergil_tooling/lib/release/merge.py`:

```python
"""Wait-poll-merge logic shared by Phases 2 and 3."""

from __future__ import annotations

from vergil_tooling.lib import github
from vergil_tooling.lib.release.context import ReleaseError

_MAX_BRANCH_UPDATES = 5


def wait_and_merge(pr_url: str, *, phase: str) -> None:
    """Wait for checks, handle behind-base, then merge."""
    updates = 0
    while True:
        if github.mergeable(pr_url) == "CONFLICTING":
            raise ReleaseError(
                phase=phase,
                command=f"gh pr view {pr_url} --json mergeable",
                message="PR has merge conflicts.",
            )

        print(f"Waiting for checks on {pr_url}...")
        github.wait_for_checks(pr_url)

        if github.merge_state_status(pr_url) != "BEHIND":
            break

        updates += 1
        if updates > _MAX_BRANCH_UPDATES:
            raise ReleaseError(
                phase=phase,
                command=f"update branch ({updates} attempts)",
                message="Branch still behind after multiple updates.",
            )
        print("Branch is behind base — updating and re-checking...")
        github.update_branch(pr_url)

    print(f"Checks passed. Merging {pr_url}...")
    github.merge(pr_url, strategy="merge")
    print("Merged.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_merge.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add merge module for wait-poll-merge logic" --body "Ref #919"
```

---

### Task 8: Bump Module

**Files:**
- Create: `src/vergil_tooling/lib/release/bump.py`
- Create: `tests/vergil_tooling/test_release_bump.py`

- [ ] **Step 1: Write tests for bump PR phase**

Create `tests/vergil_tooling/test_release_bump.py`:

```python
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.bump import merge_bump

_MOD = "vergil_tooling.lib.release.bump"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_merge_bump_finds_and_merges_pr() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + "._verify_issue_linkage"),
        patch(_MOD + ".wait_and_merge"),
    ):
        merge_bump(ctx)
    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/101"
    assert ctx.next_version == "2.1.1"


def test_merge_bump_times_out() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output", return_value=""),
        patch(_MOD + ".time.sleep"),
        pytest.raises(ReleaseError, match="timed out"),
    ):
        merge_bump(ctx)


def test_merge_bump_fails_on_missing_linkage() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "https://github.com/owner/repo/pull/101",
                "No linkage body",
            ],
        ),
        pytest.raises(ReleaseError, match="linkage"),
    ):
        merge_bump(ctx)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_bump.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement bump.py**

Create `src/vergil_tooling/lib/release/bump.py`:

```python
"""Phase 3: Poll for bump PR, verify linkage, merge."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import wait_and_merge

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

_POLL_INTERVAL = 10
_POLL_TIMEOUT = 300
_LINKAGE_RE = re.compile(r"(Ref|Fixes|Closes|Resolves)\s+#\d+", re.IGNORECASE)


def merge_bump(ctx: ReleaseContext) -> None:
    """Poll for the bump PR, verify linkage, and merge it."""
    parts = ctx.version.split(".")
    next_patch = f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    head = f"release/bump-version-{next_patch}"

    pr_url = _poll_for_bump_pr(ctx.repo, head)
    _verify_issue_linkage(ctx, pr_url)
    wait_and_merge(pr_url, phase="merge-bump")

    ctx.bump_pr_url = pr_url
    ctx.next_version = next_patch


def _poll_for_bump_pr(repo: str, head: str) -> str:
    deadline = time.monotonic() + _POLL_TIMEOUT
    while True:
        url = github.read_output(
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            head,
            "--json",
            "url",
            "--jq",
            ".[0].url",
        )
        if url:
            print(f"Bump PR found: {url}")
            return url
        if time.monotonic() >= deadline:
            raise ReleaseError(
                phase="merge-bump",
                command=f"gh pr list --head {head}",
                message=(
                    f"Bump PR on branch '{head}' did not appear within "
                    f"{_POLL_TIMEOUT} seconds. Check the version-bump-pr action."
                ),
            )
        print(f"Waiting for bump PR on {head}...")
        time.sleep(_POLL_INTERVAL)


def _verify_issue_linkage(ctx: ReleaseContext, pr_url: str) -> None:
    body = github.read_output(
        "pr", "view", pr_url, "--json", "body", "--jq", ".body",
    )
    if not _LINKAGE_RE.search(body):
        raise ReleaseError(
            phase="merge-bump",
            command=f"gh pr view {pr_url} --json body",
            message=(
                f"Bump PR {pr_url} has no issue linkage in the body. "
                f"This is a bug in the version-bump-pr action — it should "
                f"have auto-discovered tracking issue #{ctx.issue_number}."
            ),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_bump.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add bump module for version-bump PR handling" --body "Ref #919"
```

---

### Task 9: Confirm Module

**Files:**
- Create: `src/vergil_tooling/lib/release/confirm.py`
- Create: `tests/vergil_tooling/test_release_confirm.py`

- [ ] **Step 1: Write tests for confirm phase**

Create `tests/vergil_tooling/test_release_confirm.py`:

```python
from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.confirm import confirm_publish

_MOD = "vergil_tooling.lib.release.confirm"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_confirm_publish_succeeds() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",                                             # publish run id
                "https://github.com/o/r/actions/runs/12345",        # publish run url
                "67890",                                             # docs run id
                "https://github.com/o/r/actions/runs/67890",        # docs run url
                "",                                                  # tag check (no error)
                "",                                                  # develop tag check
                "https://github.com/o/r/releases/tag/v2.1.0",       # release url
            ],
        ),
        patch(_MOD + ".github.run"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        patch(_MOD + ".config.read_config") as mock_config,
    ):
        mock_config.return_value.publish.docs_workflow = "Documentation"
        confirm_publish(ctx)

    assert ctx.publish_run_id == "12345"
    assert ctx.docs_run_id == "67890"
    assert ctx.tag == "v2.1.0"


def test_confirm_publish_fails_if_tag_missing() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "https://github.com/o/r/actions/runs/12345",
                "67890",
                "https://github.com/o/r/actions/runs/67890",
            ],
        ),
        patch(_MOD + ".github.run"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".config.read_config") as mock_config,
        pytest.raises(ReleaseError, match="tag"),
    ):
        mock_config.return_value.publish.docs_workflow = "Documentation"
        confirm_publish(ctx)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_confirm.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement confirm.py**

Create `src/vergil_tooling/lib/release/confirm.py`:

```python
"""Phase 4: Watch workflows and verify publish artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, github
from vergil_tooling.lib.release.context import ReleaseError

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def confirm_publish(ctx: ReleaseContext) -> None:
    """Block on publish + docs workflows, then verify artifacts."""
    cfg = config.read_config(ctx.repo_root)
    docs_workflow = cfg.publish.docs_workflow

    _watch_workflow(ctx, "publish.yml", "publish")
    _watch_workflow(ctx, docs_workflow, "docs")
    _verify_artifacts(ctx)

    print(f"All artifacts confirmed for v{ctx.version}.")


def _watch_workflow(
    ctx: ReleaseContext, workflow: str, label: str
) -> None:
    print(f"Waiting for {workflow} on main...")
    run_id = github.read_output(
        "run",
        "list",
        "--repo",
        ctx.repo,
        "--workflow",
        workflow,
        "--branch",
        "main",
        "--limit",
        "1",
        "--json",
        "databaseId",
        "--jq",
        ".[0].databaseId",
    )
    if not run_id:
        raise ReleaseError(
            phase="confirm-publish",
            command=f"gh run list --workflow {workflow}",
            message=f"No {workflow} run found on main.",
        )

    github.run(
        "run",
        "watch",
        "--repo",
        ctx.repo,
        "--exit-status",
        run_id,
    )

    run_url = github.read_output(
        "run",
        "view",
        "--repo",
        ctx.repo,
        run_id,
        "--json",
        "url",
        "--jq",
        ".url",
    )

    if label == "publish":
        ctx.publish_run_id = run_id
        ctx.publish_run_url = run_url
    else:
        ctx.docs_run_id = run_id
        ctx.docs_run_url = run_url

    print(f"  {workflow} succeeded: {run_url}")


def _verify_artifacts(ctx: ReleaseContext) -> None:
    git.run("fetch", "--tags", "--force", "origin")

    tag = f"v{ctx.version}"
    if not git.ref_exists(tag):
        raise ReleaseError(
            phase="confirm-publish",
            command=f"git rev-parse {tag}",
            message=f"Tag {tag} does not exist after publish.",
        )
    ctx.tag = tag

    develop_tag = f"develop-v{ctx.version}"
    if not git.ref_exists(develop_tag):
        raise ReleaseError(
            phase="confirm-publish",
            command=f"git rev-parse {develop_tag}",
            message=f"Develop boundary tag {develop_tag} does not exist.",
        )
    ctx.develop_tag = develop_tag

    release_url = github.read_output(
        "release",
        "view",
        "--repo",
        ctx.repo,
        tag,
        "--json",
        "url",
        "--jq",
        ".url",
    )
    ctx.release_url = release_url
    print(f"  GitHub Release: {release_url}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_confirm.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add confirm module for workflow watching and artifact verification" --body "Ref #919"
```

---

### Task 10: Finalize Module

**Files:**
- Create: `src/vergil_tooling/lib/release/finalize.py`
- Create: `tests/vergil_tooling/test_release_finalize.py`

- [ ] **Step 1: Write tests for finalize phase**

Create `tests/vergil_tooling/test_release_finalize.py`:

```python
from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.finalize import close_and_finalize

_MOD = "vergil_tooling.lib.release.finalize"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    ctx.issue_number = 42
    ctx.issue_url = "https://github.com/owner/repo/issues/42"
    ctx.release_pr_url = "https://github.com/owner/repo/pull/100"
    ctx.bump_pr_url = "https://github.com/owner/repo/pull/101"
    ctx.tag = "v2.1.0"
    ctx.develop_tag = "develop-v2.1.0"
    ctx.release_url = "https://github.com/owner/repo/releases/tag/v2.1.0"
    ctx.publish_run_url = "https://github.com/owner/repo/actions/runs/123"
    ctx.docs_run_url = "https://github.com/owner/repo/actions/runs/456"
    return ctx


def test_close_and_finalize_succeeds() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue") as mock_close,
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=0),
        ),
    ):
        close_and_finalize(ctx)
    mock_close.assert_called_once()


def test_close_and_finalize_fails_on_finalize_error() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".close_tracking_issue"),
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(args=(), returncode=1, stderr="validation failed"),
        ),
        pytest.raises(ReleaseError, match="vrg-finalize-repo"),
    ):
        close_and_finalize(ctx)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_finalize.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement finalize.py**

Create `src/vergil_tooling/lib/release/finalize.py`:

```python
"""Phase 5: Close tracking issue and run vrg-finalize-repo."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import close_tracking_issue

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def close_and_finalize(ctx: ReleaseContext) -> None:
    """Close the tracking issue with a summary, then finalize the repo."""
    summary = _build_summary(ctx)
    close_tracking_issue(ctx, summary)
    print("Tracking issue closed.")

    print("Running vrg-finalize-repo...")
    result = subprocess.run(
        ("vrg-finalize-repo",),
        check=False,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        print(result.stdout)
    if result.returncode != 0:
        raise ReleaseError(
            phase="close-finalize",
            command="vrg-finalize-repo",
            message="vrg-finalize-repo failed.",
            detail=result.stderr or result.stdout,
        )
    print("Finalization complete.")


def _build_summary(ctx: ReleaseContext) -> str:
    lines = [
        f"## Release {ctx.version} — Summary",
        "",
        "### Pull Requests",
        f"- Release PR: {ctx.release_pr_url}",
        f"- Bump PR: {ctx.bump_pr_url}",
        "",
        "### Tags",
        f"- Release tag: `{ctx.tag}`",
        f"- Develop boundary tag: `{ctx.develop_tag}`",
        "",
        "### Artifacts",
        f"- GitHub Release: {ctx.release_url}",
        f"- publish.yml: {ctx.publish_run_url}",
        f"- docs workflow: {ctx.docs_run_url}",
    ]
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_finalize.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add finalize module for issue close and repo cleanup" --body "Ref #919"
```

---

### Task 11: Handoff Module

**Files:**
- Create: `src/vergil_tooling/lib/release/handoff.py`
- Create: `tests/vergil_tooling/test_release_handoff.py`

- [ ] **Step 1: Write tests for consumer-refresh handoff**

Create `tests/vergil_tooling/test_release_handoff.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext
from vergil_tooling.lib.release.handoff import consumer_refresh

_MOD = "vergil_tooling.lib.release.handoff"


def _ctx() -> ReleaseContext:
    return ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )


def test_consumer_refresh_templates_version(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _ctx()
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = (
            "uv tool install pkg@v<VERSION>"
        )
        consumer_refresh(ctx)
    captured = capsys.readouterr()
    assert "uv tool install pkg@v2.1.0" in captured.out


def test_consumer_refresh_none(capsys: pytest.CaptureFixture[str]) -> None:
    ctx = _ctx()
    with patch(_MOD + ".config.read_config") as mock_config:
        mock_config.return_value.publish.consumer_refresh = None
        consumer_refresh(ctx)
    captured = capsys.readouterr()
    assert "no consumer-refresh" in captured.out.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_handoff.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement handoff.py**

Create `src/vergil_tooling/lib/release/handoff.py`:

```python
"""Phase 6: Display consumer-refresh commands."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import config

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def consumer_refresh(ctx: ReleaseContext) -> None:
    """Read and display the consumer-refresh message from vergil.toml."""
    cfg = config.read_config(ctx.repo_root)
    template = cfg.publish.consumer_refresh

    if template is None:
        print(
            f"No consumer-refresh sequence is configured for {ctx.repo}. "
            f"Add [publish].consumer-refresh to vergil.toml."
        )
        return

    message = template.replace("<VERSION>", ctx.version)
    print()
    print("Consumer refresh commands:")
    print()
    print(message)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_handoff.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add handoff module for consumer-refresh display" --body "Ref #919"
```

---

### Task 12: Orchestrator

**Files:**
- Create: `src/vergil_tooling/lib/release/orchestrator.py`
- Create: `tests/vergil_tooling/test_release_orchestrator.py`

- [ ] **Step 1: Write tests for orchestrator**

Create `tests/vergil_tooling/test_release_orchestrator.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import run_release

_MOD = "vergil_tooling.lib.release.orchestrator"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_orchestrator_runs_all_phases() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".prepare") as m_prepare,
        patch(_MOD + ".merge_release") as m_merge_release,
        patch(_MOD + ".merge_bump") as m_bump,
        patch(_MOD + ".confirm_publish") as m_confirm,
        patch(_MOD + ".close_and_finalize") as m_finalize,
        patch(_MOD + ".consumer_refresh") as m_handoff,
        patch(_MOD + ".comment_phase_complete"),
    ):
        run_release(ctx)
    m_prepare.assert_called_once_with(ctx)
    m_merge_release.assert_called_once_with(ctx)
    m_bump.assert_called_once_with(ctx)
    m_confirm.assert_called_once_with(ctx)
    m_finalize.assert_called_once_with(ctx)
    m_handoff.assert_called_once_with(ctx)


def test_orchestrator_stops_on_failure_and_comments() -> None:
    ctx = _ctx()
    exc = ReleaseError(
        phase="merge-release",
        command="gh pr merge",
        message="CI failed",
    )
    with (
        patch(_MOD + ".prepare"),
        patch(_MOD + ".merge_release", side_effect=exc),
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError),
    ):
        run_release(ctx)
    m_failed.assert_called_once_with(ctx, "merge-release", exc)


def test_orchestrator_does_not_run_later_phases_on_failure() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".prepare",
            side_effect=ReleaseError(
                phase="prepare",
                command="git checkout",
                message="branch exists",
            ),
        ),
        patch(_MOD + ".merge_release") as m_merge,
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed"),
        pytest.raises(ReleaseError),
    ):
        run_release(ctx)
    m_merge.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_orchestrator.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement orchestrator.py**

Create `src/vergil_tooling/lib/release/orchestrator.py`:

```python
"""Sequential phase runner for vrg-release."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib.release.bump import merge_bump
from vergil_tooling.lib.release.confirm import confirm_publish
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.finalize import close_and_finalize
from vergil_tooling.lib.release.handoff import consumer_refresh
from vergil_tooling.lib.release.merge import wait_and_merge
from vergil_tooling.lib.release.prepare import prepare
from vergil_tooling.lib.release.tracking import (
    comment_phase_complete,
    comment_phase_failed,
)

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext


def merge_release(ctx: ReleaseContext) -> None:
    """Phase 2: merge the release PR."""
    assert ctx.release_pr_url is not None
    wait_and_merge(ctx.release_pr_url, phase="merge-release")
    ctx.release_merge_sha = "merged"


def run_release(ctx: ReleaseContext) -> None:
    """Execute the release workflow phase by phase."""
    phases: list[tuple[str, object]] = [
        ("prepare", prepare),
        ("merge-release", merge_release),
        ("merge-bump", merge_bump),
        ("confirm-publish", confirm_publish),
        ("close-finalize", close_and_finalize),
        ("consumer-refresh", consumer_refresh),
    ]

    for phase_name, phase_fn in phases:
        try:
            phase_fn(ctx)  # type: ignore[operator]
            comment_phase_complete(ctx, phase_name, "")
        except ReleaseError as exc:
            comment_phase_failed(ctx, phase_name, exc)
            raise
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_orchestrator.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```
vrg-commit --type feat --scope release --message "add orchestrator for sequential phase execution" --body "Ref #919"
```

---

### Task 13: CLI Entry Point and pyproject.toml

**Files:**
- Create: `src/vergil_tooling/bin/vrg_release.py`
- Create: `tests/vergil_tooling/test_vrg_release.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write tests for CLI entry point**

Create `tests/vergil_tooling/test_vrg_release.py`:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_release import main, parse_args

_MOD = "vergil_tooling.bin.vrg_release"


def test_parse_args_default() -> None:
    args = parse_args([])
    assert args.version_override is None


def test_parse_args_minor() -> None:
    args = parse_args(["minor"])
    assert args.version_override == "minor"


def test_parse_args_major() -> None:
    args = parse_args(["major"])
    assert args.version_override == "major"


def test_main_returns_zero_on_success() -> None:
    with (
        patch(_MOD + ".preflight") as mock_pf,
        patch(_MOD + ".run_release"),
        patch(_MOD + ".git.repo_root", return_value=Path("/tmp/repo")),
    ):
        mock_pf.return_value = object()
        result = main([])
    assert result == 0


def test_main_returns_one_on_release_error() -> None:
    from vergil_tooling.lib.release.context import ReleaseError

    with (
        patch(
            _MOD + ".preflight",
            side_effect=ReleaseError(
                phase="preflight",
                command="test",
                message="test failure",
            ),
        ),
        patch(_MOD + ".git.repo_root", return_value=Path("/tmp/repo")),
    ):
        result = main([])
    assert result == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_release.py -v`
Expected: FAIL — module does not exist

- [ ] **Step 3: Implement vrg_release.py**

Create `src/vergil_tooling/bin/vrg_release.py`:

```python
"""Mechanized release workflow — human-invoked, fully automated."""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.orchestrator import run_release
from vergil_tooling.lib.release.preflight import preflight


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Run the full release workflow from develop to main.",
    )
    parser.add_argument(
        "version_override",
        nargs="?",
        choices=("minor", "major"),
        default=None,
        help="Bump to next minor or major before releasing (default: release current version).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = git.repo_root()

    try:
        ctx = preflight(
            version_override=args.version_override,
            repo_root=repo_root,
        )
        run_release(ctx)
    except ReleaseError as exc:
        print(f"\nRelease failed in phase '{exc.phase}'.", file=sys.stderr)
        print(f"Command: {exc.command}", file=sys.stderr)
        print(f"Error: {exc}", file=sys.stderr)
        if exc.detail:
            print(f"Detail: {exc.detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_release.py -v`
Expected: ALL PASS

- [ ] **Step 5: Update pyproject.toml**

Add the new entry point and remove retired ones:

```toml
# Add:
vrg-release = "vergil_tooling.bin.vrg_release:main"

# Remove:
# vrg-check-pr-merge = "vergil_tooling.bin.vrg_check_pr_merge:main"
# vrg-merge-when-green = "vergil_tooling.bin.vrg_merge_when_green:main"
# vrg-prepare-release = "vergil_tooling.bin.vrg_prepare_release:main"
```

- [ ] **Step 6: Run full validation**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: PASS

- [ ] **Step 7: Commit**

```
vrg-commit --type feat --scope release --message "add vrg-release CLI entry point, retire absorbed tools" --body "Ref #919"
```

---

### Task 14: Clean Up Retired Modules and Tests

**Files:**
- Delete: `src/vergil_tooling/bin/vrg_prepare_release.py`
- Delete: `src/vergil_tooling/bin/vrg_merge_when_green.py`
- Delete: `src/vergil_tooling/bin/vrg_check_pr_merge.py`
- Delete: `tests/vergil_tooling/test_vrg_prepare_release.py`
- Delete: `tests/vergil_tooling/test_vrg_merge_when_green.py`
- Delete: `tests/vergil_tooling/test_vrg_check_pr_merge.py`

- [ ] **Step 1: Delete retired source files**

```bash
rm src/vergil_tooling/bin/vrg_prepare_release.py
rm src/vergil_tooling/bin/vrg_merge_when_green.py
rm src/vergil_tooling/bin/vrg_check_pr_merge.py
```

- [ ] **Step 2: Delete corresponding test files**

```bash
rm tests/vergil_tooling/test_vrg_prepare_release.py
rm tests/vergil_tooling/test_vrg_merge_when_green.py
rm tests/vergil_tooling/test_vrg_check_pr_merge.py
```

- [ ] **Step 3: Run full validation**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: PASS — no remaining imports of deleted modules

- [ ] **Step 4: Commit**

```
vrg-commit --type chore --scope release --message "remove retired CLI tools absorbed by vrg-release" --body "Ref #919

Removed: vrg-prepare-release, vrg-merge-when-green, vrg-check-pr-merge"
```

---

### Task 15: Final Validation and Documentation Update

**Files:**
- Modify: `CLAUDE.md` (update Architecture section to reflect vrg-release)

- [ ] **Step 1: Run full validation**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: PASS

- [ ] **Step 2: Update CLAUDE.md Architecture section**

In the CLI tools list under "Python Package (`src/vergil_tooling/`)":
- Add `vrg-release` with description
- Remove `vrg-prepare-release`, `vrg-merge-when-green` entries
- Update `lib/` section to include `release/` package

- [ ] **Step 3: Run validation again**

Run: `vrg-docker-run -- uv run vrg-validate`
Expected: PASS

- [ ] **Step 4: Commit**

```
vrg-commit --type docs --scope claude --message "update CLAUDE.md for vrg-release and retired tools" --body "Ref #919"
```
