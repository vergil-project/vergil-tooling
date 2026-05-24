# Release Orchestrator Refactor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Refactor the `vrg-release` orchestrator to use the independent tools (vrg-version, vrg-changelog, vrg-promote) built in Tasks 1–3, fix the non-standard Git Flow mechanics, and add CD verification on develop.

**Architecture:** Big-bang cutover — all phase modules are modified in a single branch. The orchestrator's phase sequence changes from `prepare → merge-release → merge-bump → confirm-publish → close-finalize → consumer-refresh` to `prepare → merge-release → confirm-main → back-merge-bump → confirm-develop → promote → close-finalize → consumer-refresh`. The `-X ours` merge strategy is removed, inline changelog generation is replaced with `lib/changelog`, version override moves from preflight to prepare (on the release branch), bump-PR polling is replaced with orchestrator-driven back-merge, and CD verification uses known job expectations instead of `vergil.toml` flags.

**Tech Stack:** Python 3.12, pytest, unittest.mock

**Issue:** #1069

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Modify | `src/vergil_tooling/lib/release/context.py` | Add `promote`, `develop_cd_run_id`, `develop_cd_run_url` fields |
| Modify | `src/vergil_tooling/bin/vrg_release.py` | Add `--no-promote` flag |
| Modify | `src/vergil_tooling/lib/release/preflight.py` | Remove `_detect_*` functions, use `version.show()`; remove `_apply_version_override`, compute release version without committing |
| Modify | `src/vergil_tooling/lib/release/prepare.py` | Remove `-X ours` merge; use `lib/changelog`; handle version override on release branch |
| Modify | `src/vergil_tooling/lib/release/confirm.py` | Rewrite: known job expectations, split into `confirm_main` + `confirm_develop` |
| Modify | `src/vergil_tooling/lib/release/bump.py` | Complete rewrite: orchestrator-driven back-merge+bump from main |
| Modify | `src/vergil_tooling/lib/release/orchestrator.py` | New phase sequence, promote phase, updated imports and phase details |
| Modify | `src/vergil_tooling/lib/release/finalize.py` | Update summary with new fields |
| Modify | `tests/vergil_tooling/test_release_context.py` | Test new fields |
| Modify | `tests/vergil_tooling/test_vrg_release.py` | Test `--no-promote` flag |
| Modify | `tests/vergil_tooling/test_release_preflight.py` | Remove `_detect_*` tests, update override tests |
| Modify | `tests/vergil_tooling/test_release_prepare.py` | Remove `-X ours` tests, add version override tests |
| Modify | `tests/vergil_tooling/test_release_confirm.py` | Rewrite for `confirm_main` + `confirm_develop` |
| Modify | `tests/vergil_tooling/test_release_bump.py` | Complete rewrite for back-merge+bump |
| Modify | `tests/vergil_tooling/test_release_orchestrator.py` | Update phase list, add promote tests |
| Modify | `tests/vergil_tooling/test_release_finalize.py` | Update summary assertions |

---

## Task 1: Update ReleaseContext

**Files:**
- Modify: `src/vergil_tooling/lib/release/context.py`
- Test: `tests/vergil_tooling/test_release_context.py`

### Step 1.1: Write the failing test

- [ ] Add tests for the new fields in `test_release_context.py`:

```python
def test_context_promote_defaults_true() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert ctx.promote is True


def test_context_develop_cd_fields_default_none() -> None:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    assert ctx.develop_cd_run_id is None
    assert ctx.develop_cd_run_url is None
```

### Step 1.2: Run tests to verify they fail

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_context.py -v
```

Expected: FAIL — `promote`, `develop_cd_run_id`, `develop_cd_run_url` do not exist on `ReleaseContext`.

### Step 1.3: Add fields to ReleaseContext

- [ ] In `src/vergil_tooling/lib/release/context.py`, add three fields to the `ReleaseContext` dataclass, after the existing `release_url` field:

```python
    release_url: str | None = None

    develop_cd_run_id: str | None = None
    develop_cd_run_url: str | None = None

    promote: bool = True
```

### Step 1.4: Run tests to verify they pass

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_context.py -v
```

Expected: PASS

### Step 1.5: Run full validation

- [ ] Run:

```bash
vrg-docker-run -- uv run vrg-validate
```

Expected: PASS — the new fields have defaults, so all existing code continues to work.

### Step 1.6: Commit

```bash
vrg-git add src/vergil_tooling/lib/release/context.py tests/vergil_tooling/test_release_context.py
vrg-commit --type feat --scope release --message "add promote and develop CD fields to ReleaseContext" --body "Ref #1069"
```

---

## Task 2: Add --no-promote Flag to CLI

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_release.py`
- Test: `tests/vergil_tooling/test_vrg_release.py`

### Step 2.1: Write the failing tests

- [ ] Add to `test_vrg_release.py`. First add a new import at the top:

```python
from vergil_tooling.lib.release.context import ReleaseContext
```

Then add these tests:

```python
def test_parse_args_no_promote() -> None:
    args = parse_args(["--no-promote"])
    assert args.no_promote is True
    assert args.version_override is None


def test_parse_args_default_promote() -> None:
    args = parse_args([])
    assert args.no_promote is False


def test_parse_args_no_promote_with_minor() -> None:
    args = parse_args(["--no-promote", "minor"])
    assert args.no_promote is True
    assert args.version_override == "minor"


def test_main_sets_promote_on_context() -> None:
    mock_root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + ".preflight") as mock_pf,
        patch(_MOD + ".run_release") as mock_run,
        patch(_MOD + ".git.repo_root", return_value=mock_root),
    ):
        ctx = ReleaseContext(
            repo="o/r",
            version="1.0.0",
            repo_root=mock_root,
            version_override=None,
        )
        mock_pf.return_value = ctx
        main(["--no-promote"])
    assert ctx.promote is False
    mock_run.assert_called_once_with(ctx)
```

### Step 2.2: Run tests to verify they fail

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_release.py -v
```

Expected: FAIL — `no_promote` attribute doesn't exist on the parsed args.

### Step 2.3: Add --no-promote to parser and main

- [ ] In `vrg_release.py`, add the flag to `parse_args` (after the `--verbose` argument):

```python
    parser.add_argument(
        "--no-promote",
        action="store_true",
        default=False,
        help="Skip rolling-tag promotion after release.",
    )
```

- [ ] In `main`, add `ctx.promote = not args.no_promote` after the preflight call. The relevant section becomes:

```python
        ctx = preflight(
            version_override=args.version_override,
            repo_root=repo_root,
            verbose=args.verbose,
        )
        ctx.promote = not args.no_promote
```

### Step 2.4: Run tests to verify they pass

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_release.py -v
```

Expected: PASS

### Step 2.5: Run full validation

- [ ] Run:

```bash
vrg-docker-run -- uv run vrg-validate
```

Expected: PASS

### Step 2.6: Commit

```bash
vrg-git add src/vergil_tooling/bin/vrg_release.py tests/vergil_tooling/test_vrg_release.py
vrg-commit --type feat --scope release --message "add --no-promote flag to vrg-release CLI" --body "Ref #1069"
```

---

## Task 3: Simplify Preflight

**Files:**
- Modify: `src/vergil_tooling/lib/release/preflight.py`
- Modify: `tests/vergil_tooling/test_release_preflight.py`

This task removes ~150 lines of version-detection code (the `_detect_*` functions and `_DETECTORS` list) and replaces them with a single `version.show()` call. It also removes `_apply_version_override` and `_bump_version_in_manifest`, replacing them with a pure computation that determines the release version without committing.

### Step 3.1: Write tests for the new preflight behavior

- [ ] Replace the entire contents of `tests/vergil_tooling/test_release_preflight.py` with:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.preflight import (
    _compute_release_version,
    preflight,
)

_MOD = "vergil_tooling.lib.release.preflight"


def test_preflight_success() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + "._check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.1.0"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
    ):
        ctx = preflight(version_override=None, repo_root=root)
    assert ctx.repo == "owner/repo"
    assert ctx.version == "2.1.0"
    assert ctx.version_override is None


def test_preflight_with_minor_override() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + "._check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.0.34"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
    ):
        ctx = preflight(version_override="minor", repo_root=root)
    assert ctx.version == "2.1.0"
    assert ctx.version_override == "minor"


def test_preflight_with_major_override() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + "._check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(_MOD + ".version.show", return_value="2.0.34"),
        patch(_MOD + "._check_version_not_tagged"),
        patch(_MOD + "._check_no_existing_tracking_issue"),
    ):
        ctx = preflight(version_override="major", repo_root=root)
    assert ctx.version == "3.0.0"
    assert ctx.version_override == "major"


def test_preflight_wraps_version_error() -> None:
    root = Path("/tmp/repo")  # noqa: S108
    with (
        patch(_MOD + "._check_host_prerequisites"),
        patch(_MOD + "._check_gh_auth", return_value="owner/repo"),
        patch(_MOD + "._read_and_validate_config"),
        patch(_MOD + "._check_branch_and_tree"),
        patch(_MOD + "._audit_repo_config"),
        patch(
            _MOD + ".version.show",
            side_effect=FileNotFoundError("VERSION file not found"),
        ),
        pytest.raises(ReleaseError, match="VERSION file not found"),
    ):
        preflight(version_override=None, repo_root=root)


def test_compute_release_version_minor() -> None:
    assert _compute_release_version("2.0.34", "minor") == "2.1.0"


def test_compute_release_version_major() -> None:
    assert _compute_release_version("2.0.34", "major") == "3.0.0"


def test_check_host_prerequisites_fails() -> None:
    from vergil_tooling.lib.release.preflight import _check_host_prerequisites

    with (
        patch("shutil.which", return_value=None),
        pytest.raises(ReleaseError, match="git-cliff"),
    ):
        _check_host_prerequisites()


def test_check_gh_auth_fails() -> None:
    from vergil_tooling.lib.release.preflight import _check_gh_auth

    with (
        patch(_MOD + ".github.read_output", side_effect=Exception("auth failed")),
        pytest.raises(ReleaseError, match="authentication failed"),
    ):
        _check_gh_auth()


def test_check_branch_wrong_branch() -> None:
    from vergil_tooling.lib.release.preflight import _check_branch_and_tree

    with (
        patch(_MOD + ".git.current_branch", return_value="main"),
        pytest.raises(ReleaseError, match="Must be on develop"),
    ):
        _check_branch_and_tree()


def test_check_branch_dirty_tree() -> None:
    from vergil_tooling.lib.release.preflight import _check_branch_and_tree

    with (
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.read_output", return_value="M file.py"),
        pytest.raises(ReleaseError, match="not clean"),
    ):
        _check_branch_and_tree()


def test_check_branch_not_synced() -> None:
    from vergil_tooling.lib.release.preflight import _check_branch_and_tree

    with (
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.read_output", side_effect=["", "abc1234", "def5678"]),
        patch(_MOD + ".git.run"),
        pytest.raises(ReleaseError, match="does not match"),
    ):
        _check_branch_and_tree()


def test_audit_repo_config_fails() -> None:
    from subprocess import CompletedProcess

    from vergil_tooling.lib.release.preflight import _audit_repo_config

    with (
        patch(
            _MOD + ".subprocess.run",
            return_value=CompletedProcess(
                args=(), returncode=1, stdout="non-compliant", stderr="",
            ),
        ),
        pytest.raises(ReleaseError, match="non-compliant"),
    ):
        _audit_repo_config("owner/repo")


def test_check_version_not_tagged_passes() -> None:
    from vergil_tooling.lib.release.preflight import _check_version_not_tagged

    with patch(_MOD + ".git.read_output", return_value="v2.0.33"):
        _check_version_not_tagged("2.0.34")


def test_check_version_not_tagged_fails() -> None:
    from vergil_tooling.lib.release.preflight import _check_version_not_tagged

    with (
        patch(_MOD + ".git.read_output", return_value="v2.0.34"),
        pytest.raises(ReleaseError, match="already tagged"),
    ):
        _check_version_not_tagged("2.0.34")


def test_check_version_not_tagged_no_tags() -> None:
    import subprocess

    from vergil_tooling.lib.release.preflight import _check_version_not_tagged

    with patch(
        _MOD + ".git.read_output",
        side_effect=subprocess.CalledProcessError(128, "git describe"),
    ):
        _check_version_not_tagged("2.0.34")


def test_check_no_existing_tracking_issue_passes() -> None:
    from vergil_tooling.lib.release.preflight import _check_no_existing_tracking_issue

    with patch(_MOD + ".find_existing_tracking_issue", return_value=None):
        _check_no_existing_tracking_issue("owner/repo", "2.0.34")


def test_check_no_existing_tracking_issue_fails() -> None:
    from vergil_tooling.lib.release.preflight import _check_no_existing_tracking_issue

    with (
        patch(
            _MOD + ".find_existing_tracking_issue",
            return_value="https://github.com/owner/repo/issues/99",
        ),
        pytest.raises(ReleaseError, match="already exists"),
    ):
        _check_no_existing_tracking_issue("owner/repo", "2.0.34")
```

### Step 3.2: Run tests to verify they fail

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_preflight.py -v
```

Expected: FAIL — `_compute_release_version` doesn't exist, `version.show` is not called, tests reference new module structure.

### Step 3.3: Rewrite preflight.py

- [ ] Replace the entire contents of `src/vergil_tooling/lib/release/preflight.py` with:

```python
"""Preflight checks for vrg-release."""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git, github, version
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.tracking import find_existing_tracking_issue

if TYPE_CHECKING:
    from pathlib import Path

_VERSION_OVERRIDE_FIELDS = ("minor", "major")


def preflight(
    *,
    version_override: str | None,
    repo_root: Path,
    verbose: bool = False,
) -> ReleaseContext:
    """Run all preflight checks and return an initialized ReleaseContext."""
    _check_host_prerequisites()
    repo = _check_gh_auth()
    _read_and_validate_config(repo_root)
    _check_branch_and_tree()
    _audit_repo_config(repo)

    try:
        current_version = version.show(repo_root)
    except (FileNotFoundError, version.VersionSyncError) as exc:
        raise ReleaseError(
            phase="preflight",
            command="version.show",
            message=str(exc),
        ) from exc

    if version_override in _VERSION_OVERRIDE_FIELDS:
        release_version = _compute_release_version(current_version, version_override)
    else:
        release_version = current_version

    _check_version_not_tagged(release_version)
    _check_no_existing_tracking_issue(repo, release_version)

    print(f"Preflight passed: {repo} v{release_version}")
    return ReleaseContext(
        repo=repo,
        version=release_version,
        repo_root=repo_root,
        version_override=version_override,
        verbose=verbose,
    )


def _compute_release_version(current: str, override: str) -> str:
    """Compute the target release version without modifying any files."""
    parts = current.split(".")
    if override == "minor":
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{int(parts[0]) + 1}.0.0"


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
            "repo",
            "view",
            "--json",
            "nameWithOwner",
            "--jq",
            ".nameWithOwner",
        )
    except Exception as exc:
        raise ReleaseError(
            phase="preflight",
            command="gh repo view",
            message="GitHub CLI authentication failed.",
            detail=str(exc),
        ) from exc


def _read_and_validate_config(repo_root: Path) -> None:
    config.read_config(repo_root)


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
    result = subprocess.run(  # noqa: S603
        ("vrg-github-repo-config", "audit", "--repo", repo),  # noqa: S607
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


def _check_version_not_tagged(ver: str) -> None:
    try:
        latest_tag = git.read_output(
            "describe",
            "--tags",
            "--abbrev=0",
            "--match",
            "v*",
        )
    except subprocess.CalledProcessError:
        return
    if latest_tag == f"v{ver}":
        raise ReleaseError(
            phase="preflight",
            command="git describe --tags --match v*",
            message=(
                f"Version {ver} is already tagged as {latest_tag}. "
                f"The post-publish version bump may not have run."
            ),
        )


def _check_no_existing_tracking_issue(repo: str, ver: str) -> None:
    existing = find_existing_tracking_issue(repo, ver)
    if existing is not None:
        raise ReleaseError(
            phase="preflight",
            command=f"gh issue list --search 'release: {ver}'",
            message=(
                f"A tracking issue already exists for version {ver}: {existing}\n"
                f"Close the stale issue or investigate before re-running."
            ),
        )
```

### Step 3.4: Run tests to verify they pass

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_preflight.py -v
```

Expected: PASS

### Step 3.5: Run full validation

- [ ] Run:

```bash
vrg-docker-run -- uv run vrg-validate
```

Expected: PASS

### Step 3.6: Commit

```bash
vrg-git add src/vergil_tooling/lib/release/preflight.py tests/vergil_tooling/test_release_preflight.py
vrg-commit --type refactor --scope release --message "simplify preflight: use version.show(), remove inline version detection and override commit" --body "Ref #1069"
```

---

## Task 4: Simplify Prepare Phase

**Files:**
- Modify: `src/vergil_tooling/lib/release/prepare.py`
- Modify: `tests/vergil_tooling/test_release_prepare.py`

This task removes the `-X ours` merge of `origin/main`, replaces inline changelog generation with `lib/changelog`, and handles version overrides on the release branch (bumping via `lib/version.bump()` and committing before changelog generation).

### Step 4.1: Write tests for the new prepare behavior

- [ ] Replace the entire contents of `tests/vergil_tooling/test_release_prepare.py` with:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.prepare import prepare

_MOD = "vergil_tooling.lib.release.prepare"


def _ctx(*, version_override: str | None = None) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=version_override,
    )
    ctx.issue_number = 42
    ctx.issue_url = "https://github.com/owner/repo/issues/42"
    return ctx


def test_prepare_creates_branch_and_pr() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.run"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)
    assert ctx.release_branch == "release/2.1.0"
    assert ctx.release_pr_url == "https://github.com/owner/repo/pull/100"


def test_prepare_fails_if_branch_exists() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=True),
        pytest.raises(ReleaseError, match="already exists"),
    ):
        prepare(ctx)


def test_prepare_does_not_merge_main() -> None:
    """Verify the -X ours merge of origin/main is removed."""
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

    merge_calls = [c for c in git_run_calls if "merge" in c]
    assert merge_calls == [], f"Unexpected merge calls: {merge_calls}"


def test_prepare_with_version_override() -> None:
    """Version override bumps on the release branch before changelog."""
    ctx = _ctx(version_override="minor")
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.0"),
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)

    commit_calls = [c for c in git_run_calls if c[0] == "commit"]
    assert len(commit_calls) == 1
    assert "bump version to 2.1.0" in commit_calls[0][2]


def test_prepare_without_version_override_skips_bump() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".create_tracking_issue"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump") as mock_bump,
        patch(_MOD + "._generate_changelog"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/100",
        ),
    ):
        prepare(ctx)
    mock_bump.assert_not_called()


def test_generate_changelog_uses_lib() -> None:
    from vergil_tooling.lib.release.prepare import _generate_changelog

    ctx = _ctx()
    notes_path = Path("/tmp/repo/releases/v2.1.0.md")  # noqa: S108
    with (
        patch(_MOD + ".changelog.generate_changelog") as mock_cl,
        patch(
            _MOD + ".changelog.generate_release_notes",
            return_value=notes_path,
        ) as mock_rn,
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value="M CHANGELOG.md"),
    ):
        _generate_changelog(ctx)
    mock_cl.assert_called_once_with(ctx.repo_root, ctx.version)
    mock_rn.assert_called_once_with(ctx.repo_root, ctx.version)


def test_generate_changelog_fails_on_no_changes() -> None:
    from vergil_tooling.lib.release.prepare import _generate_changelog

    ctx = _ctx()
    notes_path = Path("/tmp/repo/releases/v2.1.0.md")  # noqa: S108
    with (
        patch(_MOD + ".changelog.generate_changelog"),
        patch(
            _MOD + ".changelog.generate_release_notes",
            return_value=notes_path,
        ),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.read_output", return_value=""),
        pytest.raises(ReleaseError, match="No publishable changes"),
    ):
        _generate_changelog(ctx)
```

### Step 4.2: Run tests to verify they fail

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_prepare.py -v
```

Expected: FAIL — references `changelog`, `version` modules not yet imported in prepare.py; old code has `-X ours` merge.

### Step 4.3: Rewrite prepare.py

- [ ] Replace the entire contents of `src/vergil_tooling/lib/release/prepare.py` with:

```python
"""Phase 1: Prepare release — tracking issue, branch, changelog, PR."""

from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING

from vergil_tooling.lib import changelog, git, github, version
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.tracking import create_tracking_issue

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext


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

    if ctx.version_override is not None:
        print(f"Applying version override: {ctx.version_override}")
        version.bump(ctx.repo_root, ctx.version_override)
        git.run("add", "-A")
        git.run("commit", "-m", f"chore(release): bump version to {ctx.version}")

    _generate_changelog(ctx)

    print(f"Pushing branch: {branch}")
    git.run("push", "-u", "origin", branch)

    pr_url = _create_pr(ctx)

    git.run("checkout", "develop")

    ctx.release_branch = branch
    ctx.release_pr_url = pr_url
    print(f"Release PR created: {pr_url}")


def _generate_changelog(ctx: ReleaseContext) -> None:
    print(f"Generating changelog for v{ctx.version}")
    changelog.generate_changelog(ctx.repo_root, ctx.version)
    git.run("add", "CHANGELOG.md")

    notes_path = changelog.generate_release_notes(ctx.repo_root, ctx.version)
    git.run("add", str(notes_path))

    status = git.read_output("status", "--porcelain")
    if not status:
        raise ReleaseError(
            phase="prepare",
            command="git-cliff",
            message=(
                f"No publishable changes since the last release. "
                f"All commits after develop-v{ctx.version} are filtered "
                f"by git-cliff."
            ),
        )
    git.run("commit", "-m", f"chore(release): prepare {ctx.version}")


def _create_pr(ctx: ReleaseContext) -> str:
    title = f"release: {ctx.version}"
    body = (
        f"## Summary\n\nRelease {ctx.version}\n\n"
        f"Ref #{ctx.issue_number}\n\n"
        f"Generated with `vrg-release`\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        return github.create_pr(base="main", title=title, body_file=tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
```

### Step 4.4: Run tests to verify they pass

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_prepare.py -v
```

Expected: PASS

### Step 4.5: Run full validation

- [ ] Run:

```bash
vrg-docker-run -- uv run vrg-validate
```

Expected: PASS

### Step 4.6: Commit

```bash
vrg-git add src/vergil_tooling/lib/release/prepare.py tests/vergil_tooling/test_release_prepare.py
vrg-commit --type refactor --scope release --message "simplify prepare: use lib/changelog, remove -X ours merge, handle version override on release branch" --body "Ref #1069"
```

---

## Task 5: Rewrite Confirm Phase

**Files:**
- Modify: `src/vergil_tooling/lib/release/confirm.py`
- Modify: `tests/vergil_tooling/test_release_confirm.py`

This task replaces the `vergil.toml` flag-based CD verification with known job expectations, and adds `confirm_develop()` for the new Phase 5. The public API changes from `confirm_publish(ctx)` to `confirm_main(ctx)` and `confirm_develop(ctx)`.

### Step 5.1: Write tests for the new confirm behavior

- [ ] Replace the entire contents of `tests/vergil_tooling/test_release_confirm.py` with:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.confirm import (
    _DEVELOP_EXPECTED_JOBS,
    _MAIN_EXPECTED_JOBS,
    confirm_develop,
    confirm_main,
)
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

_MOD = "vergil_tooling.lib.release.confirm"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_main_expected_jobs() -> None:
    assert _MAIN_EXPECTED_JOBS == ("docs", "release")


def test_develop_expected_jobs() -> None:
    assert _DEVELOP_EXPECTED_JOBS == ("docs",)


def test_confirm_main_success() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "success",
                "success",
                "https://github.com/o/r/actions/runs/12345",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", return_value=True),
    ):
        confirm_main(ctx)

    assert ctx.cd_run_id == "12345"
    assert ctx.cd_run_url == "https://github.com/o/r/actions/runs/12345"
    assert ctx.tag == "v2.1.0"
    assert ctx.develop_tag == "develop-v2.1.0"
    assert ctx.release_url == "https://github.com/o/r/releases/tag/v2.1.0"


def test_confirm_main_fails_no_cd_run() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output", return_value=""),
        pytest.raises(ReleaseError, match="No CD workflow run found on main"),
    ):
        confirm_main(ctx)


def test_confirm_main_fails_job_not_found() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        pytest.raises(ReleaseError, match="not found in workflow run"),
    ):
        confirm_main(ctx)


def test_confirm_main_fails_job_not_success() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "failure",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        pytest.raises(ReleaseError, match="did not succeed"),
    ):
        confirm_main(ctx)


def test_confirm_main_fails_tag_missing() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "success",
                "success",
                "https://github.com/o/r/actions/runs/12345",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", return_value=False),
        pytest.raises(ReleaseError, match="Tag.*does not exist"),
    ):
        confirm_main(ctx)


def test_confirm_main_fails_develop_tag_missing() -> None:
    ctx = _ctx()
    ref_exists_calls = iter([True, False])
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "12345",
                "success",
                "success",
                "https://github.com/o/r/actions/runs/12345",
                "https://github.com/o/r/releases/tag/v2.1.0",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.ref_exists", side_effect=ref_exists_calls),
        pytest.raises(ReleaseError, match="Develop boundary tag"),
    ):
        confirm_main(ctx)


def test_confirm_develop_success() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "67890",
                "success",
                "https://github.com/o/r/actions/runs/67890",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
    ):
        confirm_develop(ctx)

    assert ctx.develop_cd_run_id == "67890"
    assert ctx.develop_cd_run_url == "https://github.com/o/r/actions/runs/67890"


def test_confirm_develop_fails_no_cd_run() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".github.read_output", return_value=""),
        pytest.raises(ReleaseError, match="No CD workflow run found on develop"),
    ):
        confirm_develop(ctx)


def test_confirm_develop_fails_job_not_success() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".github.read_output",
            side_effect=[
                "67890",
                "failure",
            ],
        ),
        patch(_MOD + ".watch_workflow"),
        pytest.raises(ReleaseError, match="did not succeed"),
    ):
        confirm_develop(ctx)
```

### Step 5.2: Run tests to verify they fail

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_confirm.py -v
```

Expected: FAIL — `confirm_main`, `confirm_develop`, `_MAIN_EXPECTED_JOBS`, `_DEVELOP_EXPECTED_JOBS` don't exist.

### Step 5.3: Rewrite confirm.py

- [ ] Replace the entire contents of `src/vergil_tooling/lib/release/confirm.py` with:

```python
"""Phase 3/5: Verify CD workflow and publish artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import watch_workflow

if TYPE_CHECKING:
    from vergil_tooling.lib.release.context import ReleaseContext

_CD_WORKFLOW = "cd.yml"
_MAIN_EXPECTED_JOBS = ("docs", "release")
_DEVELOP_EXPECTED_JOBS = ("docs",)


def confirm_main(ctx: ReleaseContext) -> None:
    """Watch CD on main and verify publish artifacts."""
    run_id, run_url = _watch_cd(ctx, branch="main")
    _verify_jobs(ctx, run_id, _MAIN_EXPECTED_JOBS, phase="confirm-main")

    ctx.cd_run_id = run_id
    ctx.cd_run_url = run_url

    _verify_artifacts(ctx)
    print(f"All artifacts confirmed for v{ctx.version}.")


def confirm_develop(ctx: ReleaseContext) -> None:
    """Watch CD on develop after back-merge."""
    run_id, run_url = _watch_cd(ctx, branch="develop")
    _verify_jobs(ctx, run_id, _DEVELOP_EXPECTED_JOBS, phase="confirm-develop")

    ctx.develop_cd_run_id = run_id
    ctx.develop_cd_run_url = run_url
    print("Develop CD verified.")


def _watch_cd(ctx: ReleaseContext, *, branch: str) -> tuple[str, str]:
    print(f"Waiting for {_CD_WORKFLOW} on {branch}...")
    run_id = github.read_output(
        "run",
        "list",
        "--repo",
        ctx.repo,
        "--workflow",
        _CD_WORKFLOW,
        "--branch",
        branch,
        "--limit",
        "1",
        "--json",
        "databaseId",
        "--jq",
        ".[0].databaseId",
    )
    if not run_id:
        raise ReleaseError(
            phase=f"confirm-{branch}",
            command=f"gh run list --workflow {_CD_WORKFLOW}",
            message=f"No CD workflow run found on {branch}.",
        )

    watch_workflow(ctx.repo, run_id, verbose=ctx.verbose)

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

    print(f"  CD workflow succeeded: {run_url}")
    return run_id, run_url


def _verify_jobs(
    ctx: ReleaseContext,
    run_id: str,
    expected: tuple[str, ...],
    *,
    phase: str,
) -> None:
    for job_name in expected:
        conclusion = github.read_output(
            "run",
            "view",
            "--repo",
            ctx.repo,
            run_id,
            "--json",
            "jobs",
            "--jq",
            f'.jobs[] | select(.name == "{job_name}") | .conclusion',
        )
        if not conclusion:
            raise ReleaseError(
                phase=phase,
                command=f"verify job '{job_name}'",
                message=(
                    f"Expected job '{job_name}' not found in "
                    f"workflow run {run_id}."
                ),
            )
        if conclusion != "success":
            raise ReleaseError(
                phase=phase,
                command=f"verify job '{job_name}'",
                message=(
                    f"Job '{job_name}' did not succeed "
                    f"(conclusion: '{conclusion}')."
                ),
            )
        print(f"  Job '{job_name}': success")


def _verify_artifacts(ctx: ReleaseContext) -> None:
    git.run("fetch", "--tags", "--force", "origin")

    tag = f"v{ctx.version}"
    if not git.ref_exists(tag):
        raise ReleaseError(
            phase="confirm-main",
            command=f"git rev-parse {tag}",
            message=f"Tag {tag} does not exist after publish.",
        )
    ctx.tag = tag

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

    develop_tag = f"develop-v{ctx.version}"
    if not git.ref_exists(develop_tag):
        raise ReleaseError(
            phase="confirm-main",
            command=f"git rev-parse {develop_tag}",
            message=f"Develop boundary tag {develop_tag} does not exist.",
        )
    ctx.develop_tag = develop_tag
```

### Step 5.4: Run tests to verify they pass

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_confirm.py -v
```

Expected: PASS

### Step 5.5: Commit

- [ ] Note: full validation may fail at this point because `orchestrator.py` still imports `confirm_publish`. This is expected and will be fixed in Task 8. Commit the confirm module independently.

```bash
vrg-git add src/vergil_tooling/lib/release/confirm.py tests/vergil_tooling/test_release_confirm.py
vrg-commit --type refactor --scope release --message "rewrite confirm phase: known job expectations, add confirm_develop" --body "Ref #1069"
```

---

## Task 6: Rewrite Bump Phase as Back-Merge

**Files:**
- Modify: `src/vergil_tooling/lib/release/bump.py`
- Modify: `tests/vergil_tooling/test_release_bump.py`

This task replaces the CI-action polling approach with orchestrator-driven back-merge+bump. The orchestrator creates a branch from main, bumps the version, creates a PR to develop, and merges it. The public API changes from `merge_bump(ctx)` to `back_merge_and_bump(ctx)`.

### Step 6.1: Write tests for the new bump behavior

- [ ] Replace the entire contents of `tests/vergil_tooling/test_release_bump.py` with:

```python
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError

_MOD = "vergil_tooling.lib.release.bump"


def _ctx() -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
    )
    ctx.issue_number = 42
    return ctx


def test_back_merge_creates_branch_and_pr() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    assert ctx.bump_pr_url == "https://github.com/owner/repo/pull/101"
    assert ctx.next_version == "2.1.1"


def test_back_merge_fetches_main_first() -> None:
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    fetch_idx = next(
        i for i, c in enumerate(git_run_calls) if c[0] == "fetch"
    )
    checkout_idx = next(
        i
        for i, c in enumerate(git_run_calls)
        if c[:2] == ("checkout", "-b")
    )
    assert fetch_idx < checkout_idx


def test_back_merge_commits_version_bump() -> None:
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    commit_calls = [c for c in git_run_calls if c[0] == "commit"]
    assert len(commit_calls) == 1
    assert "bump version to 2.1.1" in commit_calls[0][2]


def test_back_merge_creates_pr_to_develop() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(_MOD + ".github.create_pr") as mock_pr,
        patch(_MOD + ".wait_and_merge"),
    ):
        mock_pr.return_value = "https://github.com/owner/repo/pull/101"
        back_merge_and_bump(ctx)

    mock_pr.assert_called_once()
    call_kwargs = mock_pr.call_args
    assert call_kwargs.kwargs["base"] == "develop"


def test_back_merge_waits_and_merges() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".git.run"),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge") as mock_wm,
    ):
        back_merge_and_bump(ctx)

    mock_wm.assert_called_once_with(
        "https://github.com/owner/repo/pull/101",
        phase="back-merge-bump",
        verbose=False,
    )


def test_back_merge_pulls_develop_after_merge() -> None:
    ctx = _ctx()
    git_run_calls: list[tuple[str, ...]] = []

    def capture_git_run(*args: str) -> None:
        git_run_calls.append(args)

    with (
        patch(_MOD + ".git.run", side_effect=capture_git_run),
        patch(_MOD + ".version.bump", return_value="2.1.1"),
        patch(
            _MOD + ".github.create_pr",
            return_value="https://github.com/owner/repo/pull/101",
        ),
        patch(_MOD + ".wait_and_merge"),
    ):
        back_merge_and_bump(ctx)

    develop_checkout_idx = next(
        i
        for i, c in enumerate(git_run_calls)
        if c == ("checkout", "develop")
    )
    pull_idx = next(
        i
        for i, c in enumerate(git_run_calls)
        if c[:2] == ("pull", "origin")
    )
    assert pull_idx > develop_checkout_idx
```

### Step 6.2: Run tests to verify they fail

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_bump.py -v
```

Expected: FAIL — `back_merge_and_bump` doesn't exist.

### Step 6.3: Rewrite bump.py

- [ ] Replace the entire contents of `src/vergil_tooling/lib/release/bump.py` with:

```python
"""Phase 4: Back-merge main to develop with version bump."""

from __future__ import annotations

import tempfile
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, version
from vergil_tooling.lib.release.merge import wait_and_merge

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.release.context import ReleaseContext


def back_merge_and_bump(ctx: ReleaseContext) -> None:
    """Create back-merge branch from main, bump version, PR to develop."""
    branch = f"release/post-{ctx.version}"

    print("Fetching main...")
    git.run("fetch", "--tags", "--force", "origin", "main")

    print(f"Creating branch: {branch}")
    git.run("checkout", "-b", branch, "origin/main")

    next_ver = version.bump(ctx.repo_root)
    print(f"Bumped version to {next_ver}")
    git.run("add", "-A")
    git.run("commit", "-m", f"chore(release): bump version to {next_ver}")

    git.run("push", "-u", "origin", branch)

    pr_url = _create_bump_pr(ctx, next_ver)
    print(f"Back-merge PR created: {pr_url}")

    wait_and_merge(pr_url, phase="back-merge-bump", verbose=ctx.verbose)

    git.run("checkout", "develop")
    git.run("pull", "origin", "develop")

    ctx.bump_pr_url = pr_url
    ctx.next_version = next_ver


def _create_bump_pr(ctx: ReleaseContext, next_ver: str) -> str:
    title = (
        f"chore(release): back-merge {ctx.version} "
        f"and bump to {next_ver}"
    )
    body = (
        f"## Summary\n\n"
        f"Back-merge main after release {ctx.version} "
        f"and bump to {next_ver}.\n\n"
        f"Ref #{ctx.issue_number}\n\n"
        f"Generated with `vrg-release`\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
        f.write(body)
        tmp_path = f.name
    try:
        return github.create_pr(
            base="develop", title=title, body_file=tmp_path,
        )
    finally:
        Path(tmp_path).unlink(missing_ok=True)
```

### Step 6.4: Run tests to verify they pass

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_bump.py -v
```

Expected: PASS

### Step 6.5: Commit

- [ ] Note: full validation may fail at this point because `orchestrator.py` still imports `merge_bump`. This is expected and will be fixed in Task 8.

```bash
vrg-git add src/vergil_tooling/lib/release/bump.py tests/vergil_tooling/test_release_bump.py
vrg-commit --type refactor --scope release --message "rewrite bump phase: orchestrator-driven back-merge from main" --body "Ref #1069"
```

---

## Task 7: Update Finalize Summary

**Files:**
- Modify: `src/vergil_tooling/lib/release/finalize.py`
- Modify: `tests/vergil_tooling/test_release_finalize.py`

Update the finalize summary to include develop CD verification results and reflect the new "back-merge PR" naming.

### Step 7.1: Write the failing tests

- [ ] Add to `test_release_finalize.py`:

```python
def test_build_summary_includes_develop_cd() -> None:
    ctx = _ctx()
    ctx.develop_cd_run_url = "https://github.com/owner/repo/actions/runs/456"
    summary = _build_summary(ctx)
    assert "Develop CD" in summary
    assert "runs/456" in summary


def test_build_summary_labels_back_merge_pr() -> None:
    ctx = _ctx()
    summary = _build_summary(ctx)
    assert "Back-merge PR" in summary
```

### Step 7.2: Run tests to verify they fail

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_finalize.py -v
```

Expected: FAIL — summary says "Bump PR" not "Back-merge PR", and doesn't include develop CD.

### Step 7.3: Update _build_summary in finalize.py

- [ ] Replace the `_build_summary` function in `finalize.py` with:

```python
def _build_summary(ctx: ReleaseContext) -> str:
    lines = [
        f"## Release {ctx.version} — Summary",
        "",
        "### Pull Requests",
        f"- Release PR: {ctx.release_pr_url}",
        f"- Back-merge PR: {ctx.bump_pr_url}",
        "",
        "### Tags",
    ]
    if ctx.tag:
        lines.append(f"- Release tag: `{ctx.tag}`")
    if ctx.develop_tag:
        lines.append(f"- Develop boundary tag: `{ctx.develop_tag}`")
    lines.append("")
    lines.append("### Artifacts")
    if ctx.release_url:
        lines.append(f"- GitHub Release: {ctx.release_url}")
    if ctx.cd_run_url:
        lines.append(f"- CD workflow (main): {ctx.cd_run_url}")
    if ctx.develop_cd_run_url:
        lines.append(f"- Develop CD workflow: {ctx.develop_cd_run_url}")
    return "\n".join(lines)
```

### Step 7.4: Run tests to verify they pass

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_finalize.py -v
```

Expected: PASS. If `test_build_summary_omits_none_fields` fails on the CD label change ("CD workflow" vs "CD workflow (main)"), update its assertion from `"CD workflow"` to `"CD workflow (main)"`.

### Step 7.5: Run full validation

- [ ] Run:

```bash
vrg-docker-run -- uv run vrg-validate
```

Expected: May fail due to orchestrator import issues — will be resolved in Task 8.

### Step 7.6: Commit

```bash
vrg-git add src/vergil_tooling/lib/release/finalize.py tests/vergil_tooling/test_release_finalize.py
vrg-commit --type refactor --scope release --message "update finalize summary: back-merge PR label, develop CD" --body "Ref #1069"
```

---

## Task 8: Update Orchestrator Phase Sequence

**Files:**
- Modify: `src/vergil_tooling/lib/release/orchestrator.py`
- Modify: `tests/vergil_tooling/test_release_orchestrator.py`

This task wires everything together: new phase sequence, updated imports, promote phase, and updated phase details. After this task, `vrg-validate` must pass — this is the integration point.

### Step 8.1: Write tests for the new orchestrator

- [ ] Replace the entire contents of `tests/vergil_tooling/test_release_orchestrator.py` with:

```python
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.release.context import ReleaseContext, ReleaseError
from vergil_tooling.lib.release.orchestrator import run_release

_MOD = "vergil_tooling.lib.release.orchestrator"


def _ctx(*, promote: bool = True) -> ReleaseContext:
    ctx = ReleaseContext(
        repo="owner/repo",
        version="2.1.0",
        repo_root=Path("/tmp/repo"),  # noqa: S108
        version_override=None,
        promote=promote,
    )
    ctx.issue_number = 42
    return ctx


def test_orchestrator_runs_all_phases() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".prepare") as m_prepare,
        patch(_MOD + ".merge_release") as m_merge_release,
        patch(_MOD + ".confirm_main") as m_confirm_main,
        patch(_MOD + ".back_merge_and_bump") as m_bump,
        patch(_MOD + ".confirm_develop") as m_confirm_develop,
        patch(_MOD + "._promote_phase") as m_promote,
        patch(_MOD + ".close_and_finalize") as m_finalize,
        patch(_MOD + ".consumer_refresh") as m_handoff,
        patch(_MOD + ".comment_phase_complete"),
    ):
        run_release(ctx)
    m_prepare.assert_called_once_with(ctx)
    m_merge_release.assert_called_once_with(ctx)
    m_confirm_main.assert_called_once_with(ctx)
    m_bump.assert_called_once_with(ctx)
    m_confirm_develop.assert_called_once_with(ctx)
    m_promote.assert_called_once_with(ctx)
    m_finalize.assert_called_once_with(ctx)
    m_handoff.assert_called_once_with(ctx)


def test_promote_phase_calls_promote_when_enabled() -> None:
    from vergil_tooling.lib.release.orchestrator import _promote_phase

    ctx = _ctx(promote=True)
    with patch(_MOD + ".promote") as mock_promote:
        _promote_phase(ctx)
    mock_promote.assert_called_once_with(ctx.version)


def test_promote_phase_skips_when_disabled() -> None:
    from vergil_tooling.lib.release.orchestrator import _promote_phase

    ctx = _ctx(promote=False)
    with patch(_MOD + ".promote") as mock_promote:
        _promote_phase(ctx)
    mock_promote.assert_not_called()


def test_format_elapsed_minutes() -> None:
    from vergil_tooling.lib.release.orchestrator import _format_elapsed

    assert _format_elapsed(90) == "1m30s"
    assert _format_elapsed(125) == "2m05s"


def test_phase_details_prepare() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.release_branch = "release/2.1.0"
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    ctx.issue_url = "https://github.com/o/r/issues/42"
    details = _phase_details(ctx, "prepare")
    assert "release/2.1.0" in details
    assert "pull/100" in details
    assert "issues/42" in details


def test_phase_details_merge_release() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    details = _phase_details(ctx, "merge-release")
    assert "pull/100" in details


def test_phase_details_confirm_main() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.tag = "v2.1.0"
    ctx.release_url = "https://github.com/o/r/releases/tag/v2.1.0"
    ctx.cd_run_url = "https://github.com/o/r/actions/runs/123"
    details = _phase_details(ctx, "confirm-main")
    assert "v2.1.0" in details
    assert "runs/123" in details


def test_phase_details_back_merge_bump() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.bump_pr_url = "https://github.com/o/r/pull/101"
    ctx.next_version = "2.1.1"
    details = _phase_details(ctx, "back-merge-bump")
    assert "pull/101" in details
    assert "2.1.1" in details


def test_phase_details_confirm_develop() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    ctx.develop_cd_run_url = "https://github.com/o/r/actions/runs/456"
    details = _phase_details(ctx, "confirm-develop")
    assert "runs/456" in details


def test_phase_details_promote() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "promote")
    assert "v2.1" in details


def test_phase_details_promote_skipped() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx(promote=False)
    details = _phase_details(ctx, "promote")
    assert "skipped" in details.lower()


def test_phase_details_close_finalize() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "close-finalize")
    assert "finalized" in details.lower()


def test_phase_details_consumer_refresh() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "consumer-refresh")
    assert "Consumer refresh" in details


def test_phase_details_unknown_phase() -> None:
    from vergil_tooling.lib.release.orchestrator import _phase_details

    ctx = _ctx()
    details = _phase_details(ctx, "unknown-phase")
    assert details == ""


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


def test_orchestrator_wraps_non_release_error() -> None:
    ctx = _ctx()
    with (
        patch(
            _MOD + ".prepare",
            side_effect=subprocess.CalledProcessError(1, "git push"),
        ),
        patch(_MOD + ".comment_phase_complete"),
        patch(_MOD + ".comment_phase_failed") as m_failed,
        pytest.raises(ReleaseError),
    ):
        run_release(ctx)
    wrapped = m_failed.call_args[0][2]
    assert wrapped.phase == "prepare"
    assert "git push" in wrapped.command


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


def test_merge_release_raises_if_no_pr_url() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    with pytest.raises(ReleaseError, match="release_pr_url is not set"):
        merge_release(ctx)


def test_merge_release_calls_wait_and_merge() -> None:
    from vergil_tooling.lib.release.orchestrator import merge_release

    ctx = _ctx()
    ctx.release_pr_url = "https://github.com/o/r/pull/100"
    with patch(_MOD + ".wait_and_merge") as m_wm:
        merge_release(ctx)
    m_wm.assert_called_once_with(
        "https://github.com/o/r/pull/100",
        phase="merge-release",
        verbose=False,
    )
    assert ctx.release_merge_sha == "merged"


def test_comment_failure_raises_with_comment_phase() -> None:
    ctx = _ctx()
    with (
        patch(_MOD + ".prepare"),
        patch(
            _MOD + ".comment_phase_complete",
            side_effect=Exception("GitHub 502"),
        ),
        pytest.raises(ReleaseError) as exc_info,
    ):
        run_release(ctx)
    assert exc_info.value.phase == "comment(prepare)"
    assert exc_info.value.command == "comment_phase_complete"
```

### Step 8.2: Run tests to verify they fail

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_orchestrator.py -v
```

Expected: FAIL — old imports (`merge_bump`, `confirm_publish`), missing `_promote_phase`, wrong phase names.

### Step 8.3: Rewrite orchestrator.py

- [ ] Replace the entire contents of `src/vergil_tooling/lib/release/orchestrator.py` with:

```python
"""Sequential phase runner for vrg-release."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from vergil_tooling.lib.promote import promote
from vergil_tooling.lib.release.bump import back_merge_and_bump
from vergil_tooling.lib.release.confirm import confirm_develop, confirm_main
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
    from collections.abc import Callable

    from vergil_tooling.lib.release.context import ReleaseContext


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    return f"{minutes}m{secs:02d}s"


def merge_release(ctx: ReleaseContext) -> None:
    """Phase 2: merge the release PR."""
    if ctx.release_pr_url is None:
        raise ReleaseError(
            phase="merge-release",
            command="merge_release",
            message=(
                "release_pr_url is not set — "
                "prepare phase may not have run."
            ),
        )
    wait_and_merge(
        ctx.release_pr_url,
        phase="merge-release",
        verbose=ctx.verbose,
    )
    ctx.release_merge_sha = "merged"


def _promote_phase(ctx: ReleaseContext) -> None:
    """Phase 6: update the vX.Y rolling tag (unless --no-promote)."""
    if not ctx.promote:
        print("Skipping promote (--no-promote).")
        return
    promote(ctx.version)


def _phase_details(ctx: ReleaseContext, phase: str) -> str:
    """Build human-readable details from ctx for a completed phase."""
    lines: list[str] = []
    if phase == "prepare":
        if ctx.release_branch:
            lines.append(f"Branch: `{ctx.release_branch}`")
        if ctx.release_pr_url:
            lines.append(f"PR: {ctx.release_pr_url}")
        if ctx.issue_url:
            lines.append(f"Tracking issue: {ctx.issue_url}")
    elif phase == "merge-release":
        if ctx.release_pr_url:
            lines.append(f"Merged: {ctx.release_pr_url}")
    elif phase == "confirm-main":
        if ctx.tag:
            lines.append(f"Tag: `{ctx.tag}`")
        if ctx.release_url:
            lines.append(f"Release: {ctx.release_url}")
        if ctx.cd_run_url:
            lines.append(f"CD workflow: {ctx.cd_run_url}")
    elif phase == "back-merge-bump":
        if ctx.bump_pr_url:
            lines.append(f"Back-merge PR: {ctx.bump_pr_url}")
        if ctx.next_version:
            lines.append(f"Next version: {ctx.next_version}")
    elif phase == "confirm-develop":
        if ctx.develop_cd_run_url:
            lines.append(f"Develop CD: {ctx.develop_cd_run_url}")
    elif phase == "promote":
        if ctx.promote:
            major_minor = ".".join(ctx.version.split(".")[:2])
            lines.append(f"Promoted v{major_minor} -> v{ctx.version}")
        else:
            lines.append("Promote skipped (--no-promote).")
    elif phase == "close-finalize":
        lines.append("Tracking issue closed. Repository finalized.")
    elif phase == "consumer-refresh":
        lines.append("Consumer refresh instructions displayed.")
    return "\n".join(lines)


def run_release(ctx: ReleaseContext) -> None:
    """Execute the release workflow phase by phase."""
    phases: list[tuple[str, Callable[[ReleaseContext], None]]] = [
        ("prepare", prepare),
        ("merge-release", merge_release),
        ("confirm-main", confirm_main),
        ("back-merge-bump", back_merge_and_bump),
        ("confirm-develop", confirm_develop),
        ("promote", _promote_phase),
        ("close-finalize", close_and_finalize),
        ("consumer-refresh", consumer_refresh),
    ]

    for phase_name, phase_fn in phases:
        print(f"\n=== Phase: {phase_name} ===")
        start = time.monotonic()
        try:
            phase_fn(ctx)
        except ReleaseError as exc:
            elapsed = time.monotonic() - start
            print(
                f"=== {phase_name}: FAILED "
                f"({_format_elapsed(elapsed)}) ==="
            )
            comment_phase_failed(ctx, phase_name, exc)
            raise
        except Exception as exc:
            elapsed = time.monotonic() - start
            print(
                f"=== {phase_name}: FAILED "
                f"({_format_elapsed(elapsed)}) ==="
            )
            wrapped = ReleaseError(
                phase=phase_name,
                command=str(
                    getattr(exc, "cmd", type(exc).__name__)
                ),
                message=str(exc),
                detail=(
                    getattr(exc, "stderr", None)
                    or getattr(exc, "stdout", None)
                ),
            )
            comment_phase_failed(ctx, phase_name, wrapped)
            raise wrapped from exc
        elapsed = time.monotonic() - start
        print(f"=== {phase_name}: done ({_format_elapsed(elapsed)}) ===")
        try:
            comment_phase_complete(
                ctx,
                phase_name,
                _phase_details(ctx, phase_name),
            )
        except Exception as exc:
            raise ReleaseError(
                phase=f"comment({phase_name})",
                command="comment_phase_complete",
                message=str(exc),
                detail=(
                    getattr(exc, "stderr", None)
                    or getattr(exc, "stdout", None)
                ),
            ) from exc
```

### Step 8.4: Run tests to verify they pass

- [ ] Run:

```bash
vrg-docker-run -- uv run pytest tests/vergil_tooling/test_release_orchestrator.py -v
```

Expected: PASS

### Step 8.5: Run full validation

- [ ] Run:

```bash
vrg-docker-run -- uv run vrg-validate
```

Expected: PASS — all modules now reference the correct imports and the full test suite should be green. This is the critical integration checkpoint.

### Step 8.6: Commit

```bash
vrg-git add src/vergil_tooling/lib/release/orchestrator.py tests/vergil_tooling/test_release_orchestrator.py
vrg-commit --type refactor --scope release --message "update orchestrator: new phase sequence with confirm-main, back-merge, confirm-develop, promote" --body "Ref #1069"
```

---

## Cutover Sequence

After all 8 tasks are implemented and validated:

1. **Merge vergil-tooling changes** — the new `vrg-release` is ready but not yet exercised.

2. **Merge vergil-actions changes** (separate repo, separate PR):
   - Remove `version-bump-pr` action invocation from `cd-release.yml`
   - Remove inline rolling-tag force-update from `cd-release.yml`
   - Verify `cd-release.yml` still creates tags and GitHub Releases correctly

3. **Release vergil-tooling using the new workflow** — first real exercise of the refactored orchestrator.

4. **Update all managed repos** to the new vergil-actions version.

---

## Notes

- The `handoff.py`, `merge.py`, `tracking.py`, and `subprocess.py` modules are **unchanged** by this refactor.
- The vergil-actions changes are tracked separately and are not part of this implementation plan.
- The `_read_and_validate_config` function in preflight is kept (it validates vergil.toml exists and parses correctly). Its return value is no longer used since `_apply_version_override` was removed.
