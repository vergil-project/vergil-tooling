# vrg-update-deps Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a working, human-run `vrg-update-deps` that, in this repo, runs
the Python/uv dependency upgrade end-to-end inside a managed git worktree —
branch → upgrade → validate → PR → merge → finalize — with no-op and abort
handling.

**Architecture:** A declarative stage pipeline modeled on
`lib/release/orchestrator.py`, driving an extensible updater registry. All work
happens in a managed worktree created off `develop` (the root checkout is never
switched), so a parallel agent can branch off `develop` safely. Phase 1
implements the driver, the worktree mechanism, a single Python/uv updater, and
the full PR lifecycle (reusing `pr_merge` and `github`).

**Tech Stack:** Python 3.12+, `argparse`, the in-repo `progress` framework
(`lib/progress.py`), `lib/git`, `lib/github`, `lib/pr_merge`, `lib/config`,
`lib/identity_mode`; pytest + `unittest.mock` for tests.

**Spec:** `docs/specs/2026-06-10-vrg-update-deps-design.md`

---

## Scope of this plan

In scope (Phase 1): the generic driver, the managed-worktree mechanism, the
updater interface + registry, the Python/uv updater, preflight (clean + synced
develop), single-shot validation, the PR create/merge/finalize lifecycle,
no-op (remove-worktree-no-PR) and abort (leave-worktree) semantics, and
human-only identity gating.

Explicitly **deferred to later plans** (do not implement here): the
`[dependency-update]` config section and override/pin schema, a per-run/standing
deps tracking issue, the vergil ecosystem normalize/bump updater, the
Ruby/Go/Java/JavaScript updaters, third-party GitHub Actions SHA-pinning, the
Docker and VM extensions, warn-only dependencies, the `vrg-release --with-deps`
integration, and the `vrg-release` worktree retrofit (tracked in #1578).

## File structure

| File | Responsibility |
| --- | --- |
| `src/vergil_tooling/lib/update_deps/__init__.py` | Package marker |
| `src/vergil_tooling/lib/update_deps/context.py` | `UpdateDepsContext` dataclass + `UpdateDepsError` |
| `src/vergil_tooling/lib/update_deps/updater.py` | `UpdateResult`, `Updater` protocol, `applicable_updaters()` |
| `src/vergil_tooling/lib/update_deps/updaters/__init__.py` | Package marker |
| `src/vergil_tooling/lib/update_deps/updaters/python_uv.py` | `PythonUvUpdater` — `uv lock --upgrade` in container |
| `src/vergil_tooling/lib/update_deps/worktree.py` | `create_worktree` / `remove_worktree` |
| `src/vergil_tooling/lib/update_deps/preflight.py` | Clean/synced-develop checks; create worktree + `chdir` |
| `src/vergil_tooling/lib/update_deps/validate.py` | Run `vrg-container-run -- vrg-validate` |
| `src/vergil_tooling/lib/update_deps/pr.py` | `prepare_pr`, `merge_pr`, `cleanup_worktree` |
| `src/vergil_tooling/lib/update_deps/orchestrator.py` | `UpdateDepsState`, `build_stages()`, stage fns |
| `src/vergil_tooling/bin/vrg_update_deps.py` | CLI entry point + identity gate |
| `pyproject.toml` | Register the `vrg-update-deps` console script |
| `tests/vergil_tooling/test_update_deps_*.py` | One test module per source module |

The branch is `chore/dep-update-<YYYYMMDD>`; the worktree lives at
`<repo_root>/.worktrees/chore-dep-update-<YYYYMMDD>`.

**Worktree mechanics:** preflight is the only stage that runs in the root
checkout. After it creates the worktree it calls `os.chdir(worktree_path)`;
every later stage (updaters, validate, PR push, merge) therefore runs git and
validation in the worktree (`git.run`/`git.read_output` use the cwd;
`vrg-container-run` mounts the cwd). `cleanup_worktree` `chdir`s back to
`ctx.repo_root` before removing the worktree. On abort, the pipeline stops
before cleanup, leaving the worktree in place.

---

### Task 1: Context and error types

**Files:**
- Create: `src/vergil_tooling/lib/update_deps/__init__.py`
- Create: `src/vergil_tooling/lib/update_deps/context.py`
- Test: `tests/vergil_tooling/test_update_deps_context.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_update_deps_context.py
from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError


def test_context_defaults() -> None:
    ctx = UpdateDepsContext(repo="owner/repo", repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.branch is None
    assert ctx.worktree_path is None
    assert ctx.pr_url is None
    assert ctx.any_changes is False
    assert ctx.results == []


def test_update_deps_error_carries_fields() -> None:
    err = UpdateDepsError(phase="preflight", command="git status", message="dirty", detail="x")
    assert err.phase == "preflight"
    assert err.command == "git status"
    assert err.detail == "x"
    assert str(err) == "dirty"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_context.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.update_deps'`

- [ ] **Step 3: Write the package marker and the module**

```python
# src/vergil_tooling/lib/update_deps/__init__.py
"""Mechanized dependency update — human-invoked, deterministic."""
```

```python
# src/vergil_tooling/lib/update_deps/context.py
"""Shared state and error type for the vrg-update-deps pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from vergil_tooling.lib.update_deps.updater import UpdateResult


@dataclass
class UpdateDepsContext:
    """State that flows through every vrg-update-deps stage."""

    repo: str
    repo_root: Path
    branch: str | None = None
    worktree_path: Path | None = None
    pr_url: str | None = None
    any_changes: bool = False
    results: list[UpdateResult] = field(default_factory=list)


class UpdateDepsError(Exception):
    """Raised when a vrg-update-deps stage fails."""

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

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_context.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/update_deps/__init__.py \
  src/vergil_tooling/lib/update_deps/context.py \
  tests/vergil_tooling/test_update_deps_context.py
vrg-git commit -m "feat(update-deps): add pipeline context and error types"
```

---

### Task 2: Updater interface and registry

**Files:**
- Create: `src/vergil_tooling/lib/update_deps/updater.py`
- Test: `tests/vergil_tooling/test_update_deps_updater.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_update_deps_updater.py
from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.update_deps.context import UpdateDepsContext
from vergil_tooling.lib.update_deps.updater import UpdateResult, applicable_updaters


class _Yes:
    name = "yes"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        return UpdateResult(updater=self.name, changed=False, summary="", commit_message="")


class _No:
    name = "no"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return False

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        raise AssertionError("should not run")


def test_applicable_filters_by_applies() -> None:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    picked = applicable_updaters(ctx, registry=[_Yes(), _No()])
    assert [u.name for u in picked] == ["yes"]


def test_update_result_defaults() -> None:
    result = UpdateResult(updater="x", changed=True, summary="s", commit_message="m")
    assert result.warnings == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_updater.py -v`
Expected: FAIL — `ModuleNotFoundError: ...updater`

- [ ] **Step 3: Write the module**

```python
# src/vergil_tooling/lib/update_deps/updater.py
"""Updater interface, result type, and registry for vrg-update-deps.

An updater upgrades one dependency category at its source of truth. It never
runs validation, commits, or touches git history — the driver owns those.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext


@dataclass
class UpdateResult:
    """What an updater changed (or didn't) in one run."""

    updater: str
    changed: bool
    summary: str
    commit_message: str
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class Updater(Protocol):
    """One dependency-category updater."""

    name: str

    def applies(self, ctx: UpdateDepsContext) -> bool:
        """True when this repo has the surface this updater handles."""
        ...

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        """Upgrade at the source of truth; report what changed."""
        ...


def applicable_updaters(
    ctx: UpdateDepsContext,
    *,
    registry: list[Updater],
) -> list[Updater]:
    """Return registry members whose ``applies`` is true for this repo."""
    return [u for u in registry if u.applies(ctx)]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_updater.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/update_deps/updater.py \
  tests/vergil_tooling/test_update_deps_updater.py
vrg-git commit -m "feat(update-deps): add updater protocol, result type, registry"
```

---

### Task 3: Python/uv updater

**Files:**
- Create: `src/vergil_tooling/lib/update_deps/updaters/__init__.py`
- Create: `src/vergil_tooling/lib/update_deps/updaters/python_uv.py`
- Test: `tests/vergil_tooling/test_update_deps_python_uv.py`

Applies when both `pyproject.toml` and `uv.lock` exist (checked against
`ctx.repo_root`, which after preflight's `chdir` is the worktree). `apply()`
runs `uv lock --upgrade` in the dev container, then reports `changed=True` iff
`uv.lock` is now dirty.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_update_deps_python_uv.py
from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.update_deps.context import UpdateDepsContext
from vergil_tooling.lib.update_deps.updaters.python_uv import PythonUvUpdater

_MOD = "vergil_tooling.lib.update_deps.updaters.python_uv"


def _ctx(root: Path) -> UpdateDepsContext:
    return UpdateDepsContext(repo="o/r", repo_root=root)


def test_applies_true_when_pyproject_and_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "uv.lock").write_text("")
    assert PythonUvUpdater().applies(_ctx(tmp_path)) is True


def test_applies_false_without_lock(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    assert PythonUvUpdater().applies(_ctx(tmp_path)) is False


def test_apply_reports_changed_when_lock_dirty(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: calls.append(list(cmd)) or 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: " M uv.lock")
    result = PythonUvUpdater().apply(_ctx(tmp_path))
    assert calls == [["vrg-container-run", "--", "uv", "lock", "--upgrade"]]
    assert result.changed is True
    assert result.commit_message == "chore(deps): uv lock --upgrade"


def test_apply_reports_unchanged_when_lock_clean(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: 0)
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: "")
    result = PythonUvUpdater().apply(_ctx(tmp_path))
    assert result.changed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_python_uv.py -v`
Expected: FAIL — `ModuleNotFoundError: ...python_uv`

- [ ] **Step 3: Write the modules**

```python
# src/vergil_tooling/lib/update_deps/updaters/__init__.py
"""Built-in updaters for vrg-update-deps."""
```

```python
# src/vergil_tooling/lib/update_deps/updaters/python_uv.py
"""Python language-library updater: ``uv lock --upgrade`` in the dev container."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import git, progress
from vergil_tooling.lib.update_deps.updater import UpdateResult

if TYPE_CHECKING:
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

_COMMIT_MESSAGE = "chore(deps): uv lock --upgrade"


class PythonUvUpdater:
    """Upgrade Python dependencies within their declared constraints."""

    name = "python-uv"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        root = ctx.repo_root
        return (root / "pyproject.toml").is_file() and (root / "uv.lock").is_file()

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        progress.run(["vrg-container-run", "--", "uv", "lock", "--upgrade"])
        dirty = bool(git.read_output("status", "--porcelain", "uv.lock").strip())
        return UpdateResult(
            updater=self.name,
            changed=dirty,
            summary="uv lock --upgrade" if dirty else "uv lock --upgrade (no changes)",
            commit_message=_COMMIT_MESSAGE,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_python_uv.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/update_deps/updaters/ \
  tests/vergil_tooling/test_update_deps_python_uv.py
vrg-git commit -m "feat(update-deps): add Python/uv language updater"
```

---

### Task 4: Managed-worktree helper

**Files:**
- Create: `src/vergil_tooling/lib/update_deps/worktree.py`
- Test: `tests/vergil_tooling/test_update_deps_worktree.py`

`create_worktree` makes a worktree under `.worktrees/` on a new branch off a
base ref; `remove_worktree` force-removes it. Both are thin wrappers over
`git worktree`, kept isolated so `vrg-release` can reuse them later (#1578).

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_update_deps_worktree.py
from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.worktree import create_worktree, remove_worktree

_MOD = "vergil_tooling.lib.update_deps.worktree"


def test_create_worktree_adds_off_base(tmp_path: Path, monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    path = create_worktree(tmp_path, branch="chore/dep-update-20260610", base="develop")
    expected = tmp_path / ".worktrees" / "chore-dep-update-20260610"
    assert path == expected
    assert (
        "worktree",
        "add",
        "-b",
        "chore/dep-update-20260610",
        str(expected),
        "develop",
    ) in runs


def test_create_worktree_rejects_existing_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    target = tmp_path / ".worktrees" / "chore-dep-update-20260610"
    target.mkdir(parents=True)
    with pytest.raises(UpdateDepsError, match="already exists"):
        create_worktree(tmp_path, branch="chore/dep-update-20260610", base="develop")


def test_remove_worktree_force_removes(tmp_path: Path, monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    remove_worktree(tmp_path / ".worktrees" / "x")
    assert ("worktree", "remove", "--force", str(tmp_path / ".worktrees" / "x")) in runs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_worktree.py -v`
Expected: FAIL — `ModuleNotFoundError: ...worktree`

- [ ] **Step 3: Write the module**

```python
# src/vergil_tooling/lib/update_deps/worktree.py
"""Managed git-worktree create/remove for automated workflows.

Kept independent of vrg-update-deps internals so vrg-release can adopt the same
mechanism (#1578)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.update_deps.context import UpdateDepsError

if TYPE_CHECKING:
    from pathlib import Path


def create_worktree(repo_root: Path, *, branch: str, base: str) -> Path:
    """Create a worktree under ``.worktrees/`` on a new ``branch`` off ``base``."""
    path = repo_root / ".worktrees" / branch.replace("/", "-")
    if path.exists():
        raise UpdateDepsError(
            phase="preflight",
            command=f"git worktree add {path}",
            message=f"Worktree path already exists: {path}. Remove it and re-run.",
        )
    git.run("worktree", "add", "-b", branch, str(path), base)
    return path


def remove_worktree(path: Path) -> None:
    """Force-remove a managed worktree (the branch ref is left for the caller)."""
    git.run("worktree", "remove", "--force", str(path))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_worktree.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/update_deps/worktree.py \
  tests/vergil_tooling/test_update_deps_worktree.py
vrg-git commit -m "feat(update-deps): add managed-worktree create/remove helper"
```

---

### Task 5: Preflight (clean+synced develop, create worktree, chdir)

**Files:**
- Create: `src/vergil_tooling/lib/update_deps/preflight.py`
- Test: `tests/vergil_tooling/test_update_deps_preflight.py`

Asserts on-develop + clean tree + local develop in sync with `origin/develop`
(reusing the release `check_gh_auth` for the auth check), then creates the
worktree and `chdir`s into it.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_update_deps_preflight.py
from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.preflight import preflight

_MOD = "vergil_tooling.lib.update_deps.preflight"


def _sync_ok(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: "deadbeef" if "rev-parse" in a else "")
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    monkeypatch.setattr(_MOD + "._today", lambda: "20260610")


def test_preflight_creates_worktree_and_chdirs(monkeypatch) -> None:
    _sync_ok(monkeypatch)
    chdirs: list[Path] = []
    made: dict[str, object] = {}
    monkeypatch.setattr(
        _MOD + ".create_worktree",
        lambda root, *, branch, base: made.update(branch=branch, base=base)
        or Path("/tmp/repo/.worktrees/chore-dep-update-20260610"),  # noqa: S108
    )
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(Path(p)))
    ctx = preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
    assert ctx.repo == "owner/repo"
    assert ctx.branch == "chore/dep-update-20260610"
    assert made == {"branch": "chore/dep-update-20260610", "base": "develop"}
    assert ctx.worktree_path == Path("/tmp/repo/.worktrees/chore-dep-update-20260610")  # noqa: S108
    assert chdirs == [Path("/tmp/repo/.worktrees/chore-dep-update-20260610")]  # noqa: S108


def test_preflight_rejects_non_develop(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "feature/x")
    with pytest.raises(UpdateDepsError, match="Must be on develop"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_rejects_dirty_tree(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: " M file.py")
    with pytest.raises(UpdateDepsError, match="not clean"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108


def test_preflight_rejects_out_of_sync_develop(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".check_gh_auth", lambda: "owner/repo")
    monkeypatch.setattr(_MOD + ".config.read_config", lambda root: None)
    monkeypatch.setattr(_MOD + ".git.current_branch", lambda: "develop")
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    outs = iter(["", "local-sha", "remote-sha"])  # status, HEAD, origin/develop
    monkeypatch.setattr(_MOD + ".git.read_output", lambda *a: next(outs))
    with pytest.raises(UpdateDepsError, match="sync"):
        preflight(repo_root=Path("/tmp/repo"))  # noqa: S108
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_preflight.py -v`
Expected: FAIL — `ModuleNotFoundError: ...preflight`

- [ ] **Step 3: Write the module**

```python
# src/vergil_tooling/lib/update_deps/preflight.py
"""Preflight checks and managed-worktree creation for vrg-update-deps."""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING

from vergil_tooling.lib import config, git
from vergil_tooling.lib.release.preflight import check_gh_auth
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.worktree import create_worktree

if TYPE_CHECKING:
    from pathlib import Path


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")  # noqa: DTZ005


def preflight(*, repo_root: Path) -> UpdateDepsContext:
    """Validate preconditions and create the managed worktree."""
    repo = check_gh_auth()
    config.read_config(repo_root)

    branch_now = git.current_branch()
    if branch_now != "develop":
        raise UpdateDepsError(
            phase="preflight",
            command="git rev-parse --abbrev-ref HEAD",
            message=f"Must be on develop branch (currently on '{branch_now}').",
        )
    if git.read_output("status", "--porcelain"):
        raise UpdateDepsError(
            phase="preflight",
            command="git status --porcelain",
            message="Working tree is not clean.",
        )
    git.run("fetch", "origin", "develop")
    local_sha = git.read_output("rev-parse", "HEAD")
    remote_sha = git.read_output("rev-parse", "origin/develop")
    if local_sha != remote_sha:
        raise UpdateDepsError(
            phase="preflight",
            command="git rev-parse HEAD vs origin/develop",
            message=(
                f"Local develop ({local_sha[:8]}) is not in sync with "
                f"origin/develop ({remote_sha[:8]}). Pull latest first."
            ),
        )

    branch = f"chore/dep-update-{_today()}"
    worktree_path = create_worktree(repo_root, branch=branch, base="develop")
    os.chdir(worktree_path)
    print(f"Preflight passed: {repo} — worktree {worktree_path}")
    return UpdateDepsContext(
        repo=repo,
        repo_root=repo_root,
        branch=branch,
        worktree_path=worktree_path,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_preflight.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/update_deps/preflight.py \
  tests/vergil_tooling/test_update_deps_preflight.py
vrg-git commit -m "feat(update-deps): add preflight with synced-develop checks and worktree creation"
```

---

### Task 6: Validation stage

**Files:**
- Create: `src/vergil_tooling/lib/update_deps/validate.py`
- Test: `tests/vergil_tooling/test_update_deps_validate.py`

Runs `vrg-container-run -- vrg-validate` in the worktree (the repo's
`[validation]` override is applied inside `vrg-container-run`). A nonzero exit
raises `UpdateDepsError` with the captured output.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_update_deps_validate.py
from __future__ import annotations

import subprocess

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.validate import run_validation

_MOD = "vergil_tooling.lib.update_deps.validate"


def test_run_validation_invokes_container_run(monkeypatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setattr(_MOD + ".progress.run", lambda cmd, **kw: seen.append(list(cmd)) or 0)
    run_validation()
    assert seen == [["vrg-container-run", "--", "vrg-validate"]]


def test_run_validation_raises_on_failure(monkeypatch) -> None:
    def _boom(cmd, **kw):
        raise subprocess.CalledProcessError(1, cmd, output="boom-out", stderr="boom-err")

    monkeypatch.setattr(_MOD + ".progress.run", _boom)
    with pytest.raises(UpdateDepsError, match="Validation failed"):
        run_validation()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_validate.py -v`
Expected: FAIL — `ModuleNotFoundError: ...validate`

- [ ] **Step 3: Write the module**

```python
# src/vergil_tooling/lib/update_deps/validate.py
"""Single-shot validation stage for vrg-update-deps."""

from __future__ import annotations

import subprocess

from vergil_tooling.lib import progress
from vergil_tooling.lib.update_deps.context import UpdateDepsError

_VALIDATE_CMD = ["vrg-container-run", "--", "vrg-validate"]


def run_validation() -> None:
    """Run the canonical validation command in the cwd (the worktree); raise on failure."""
    try:
        progress.run(_VALIDATE_CMD)
    except subprocess.CalledProcessError as exc:
        raise UpdateDepsError(
            phase="validate",
            command=" ".join(_VALIDATE_CMD),
            message="Validation failed after dependency updates.",
            detail=(exc.stderr or exc.output or ""),
        ) from exc
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_validate.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/update_deps/validate.py \
  tests/vergil_tooling/test_update_deps_validate.py
vrg-git commit -m "feat(update-deps): add single-shot validation stage"
```

---

### Task 7: PR lifecycle helpers

**Files:**
- Create: `src/vergil_tooling/lib/update_deps/pr.py`
- Test: `tests/vergil_tooling/test_update_deps_pr.py`

`prepare_pr` pushes the worktree branch and opens the PR to `develop`;
`merge_pr` reuses `pr_merge.wait_and_merge`; `cleanup_worktree` `chdir`s back to
the root, removes the worktree, and deletes the local branch (used by both the
no-op and success paths).

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_update_deps_pr.py
from __future__ import annotations

from pathlib import Path

from vergil_tooling.lib.update_deps.context import UpdateDepsContext
from vergil_tooling.lib.update_deps.pr import build_pr_body, cleanup_worktree, merge_pr, prepare_pr
from vergil_tooling.lib.update_deps.updater import UpdateResult

_MOD = "vergil_tooling.lib.update_deps.pr"


def _ctx() -> UpdateDepsContext:
    ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    ctx.branch = "chore/dep-update-20260610"
    ctx.worktree_path = Path("/tmp/r/.worktrees/chore-dep-update-20260610")  # noqa: S108
    ctx.results = [UpdateResult(updater="python-uv", changed=True, summary="uv lock --upgrade", commit_message="m")]
    return ctx


def test_build_pr_body_lists_changed_updaters() -> None:
    body = build_pr_body(_ctx())
    assert "python-uv" in body
    assert "uv lock --upgrade" in body
    assert "Ref #1379" in body


def test_prepare_pr_pushes_and_creates(monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    monkeypatch.setattr(_MOD + ".github.create_pr", lambda **kw: "https://x/pr/1")
    ctx = _ctx()
    prepare_pr(ctx)
    assert ("push", "-u", "origin", "chore/dep-update-20260610") in runs
    assert ctx.pr_url == "https://x/pr/1"


def test_merge_pr_calls_wait_and_merge(monkeypatch) -> None:
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        _MOD + ".pr_merge.wait_and_merge",
        lambda pr, *, strategy, wait_checks=None: seen.update(pr=pr, strategy=strategy),
    )
    ctx = _ctx()
    ctx.pr_url = "https://x/pr/1"
    merge_pr(ctx)
    assert seen == {"pr": "https://x/pr/1", "strategy": "merge"}


def test_cleanup_worktree_chdir_remove_delete(monkeypatch) -> None:
    chdirs: list[Path] = []
    removed: list[Path] = []
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".os.chdir", lambda p: chdirs.append(Path(p)))
    monkeypatch.setattr(_MOD + ".remove_worktree", lambda p: removed.append(p))
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    ctx = _ctx()
    cleanup_worktree(ctx)
    assert chdirs == [Path("/tmp/r")]  # noqa: S108
    assert removed == [ctx.worktree_path]
    assert ("branch", "-D", "chore/dep-update-20260610") in runs
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_pr.py -v`
Expected: FAIL — `ModuleNotFoundError: ...pr`

- [ ] **Step 3: Write the module**

```python
# src/vergil_tooling/lib/update_deps/pr.py
"""PR create / merge / worktree-cleanup helpers for vrg-update-deps."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from vergil_tooling.lib import git, github, pr_merge
from vergil_tooling.lib.release.subprocess import wait_for_checks
from vergil_tooling.lib.update_deps.context import UpdateDepsError
from vergil_tooling.lib.update_deps.worktree import remove_worktree

if TYPE_CHECKING:
    from vergil_tooling.lib.update_deps.context import UpdateDepsContext

_TITLE = "chore(deps): dependency update sweep"


def build_pr_body(ctx: UpdateDepsContext) -> str:
    """Build a PR body listing each updater that changed something."""
    lines = ["Mechanized dependency update (`vrg-update-deps`).", "", "## Updated", ""]
    for result in ctx.results:
        if result.changed:
            lines.append(f"- **{result.updater}** — {result.summary}")
    warnings = [w for r in ctx.results for w in r.warnings]
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {w}" for w in warnings)
    lines.extend(["", "Ref #1379"])
    return "\n".join(lines) + "\n"


def prepare_pr(ctx: UpdateDepsContext) -> None:
    """Push the worktree branch and open the PR to develop."""
    if ctx.branch is None:
        raise UpdateDepsError(
            phase="prepare-pr",
            command="prepare_pr",
            message="No branch on context — preflight did not run.",
        )
    git.run("push", "-u", "origin", ctx.branch)
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(build_pr_body(ctx))
        body_path = Path(handle.name)
    try:
        ctx.pr_url = github.create_pr(base="develop", title=_TITLE, body_file=str(body_path))
    finally:
        body_path.unlink(missing_ok=True)
    print(f"Opened PR: {ctx.pr_url}")


def merge_pr(ctx: UpdateDepsContext) -> None:
    """Wait for checks and merge the dependency-update PR."""
    if ctx.pr_url is None:
        raise UpdateDepsError(
            phase="merge",
            command="merge_pr",
            message="No PR URL on context — prepare-pr did not run.",
        )
    try:
        pr_merge.wait_and_merge(ctx.pr_url, strategy="merge", wait_checks=wait_for_checks)
    except pr_merge.MergeAbortError as exc:
        raise UpdateDepsError(
            phase="merge",
            command="pr_merge.wait_and_merge",
            message=str(exc),
        ) from exc


def cleanup_worktree(ctx: UpdateDepsContext) -> None:
    """Return to the root checkout and remove the managed worktree + branch."""
    if ctx.worktree_path is None:
        return
    os.chdir(ctx.repo_root)
    remove_worktree(ctx.worktree_path)
    if ctx.branch is not None:
        git.run("branch", "-D", ctx.branch)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_pr.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/update_deps/pr.py \
  tests/vergil_tooling/test_update_deps_pr.py
vrg-git commit -m "feat(update-deps): add PR create/merge and worktree-cleanup helpers"
```

---

### Task 8: Orchestrator (stages, state, no-op/abort)

**Files:**
- Create: `src/vergil_tooling/lib/update_deps/orchestrator.py`
- Test: `tests/vergil_tooling/test_update_deps_orchestrator.py`

`run_updaters_stage` runs each applicable updater, commits the ones that changed
(`git add -A` is safe — the worktree is isolated and clean), and records
`ctx.any_changes`. Downstream stages guard on `ctx.any_changes`:
validate/prepare/merge no-op when nothing changed; finalize removes the worktree
in both the no-op and success paths. `validate` is `fail_fast` (a red
validation aborts before finalize, leaving the worktree); `finalize` is
`fail_defer`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_update_deps_orchestrator.py
from __future__ import annotations

from pathlib import Path

import pytest

from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.orchestrator import (
    UpdateDepsState,
    build_stages,
    finalize_stage,
    run_updaters_stage,
    validate_stage,
)
from vergil_tooling.lib.update_deps.updater import UpdateResult

_MOD = "vergil_tooling.lib.update_deps.orchestrator"


class _Changed:
    name = "changed"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        return UpdateResult(updater="changed", changed=True, summary="s", commit_message="chore(deps): s")


class _NoChange:
    name = "nochange"

    def applies(self, ctx: UpdateDepsContext) -> bool:
        return True

    def apply(self, ctx: UpdateDepsContext) -> UpdateResult:
        return UpdateResult(updater="nochange", changed=False, summary="", commit_message="")


def _state() -> UpdateDepsState:
    state = UpdateDepsState(repo_root=Path("/tmp/r"))  # noqa: S108
    state.ctx = UpdateDepsContext(repo="o/r", repo_root=Path("/tmp/r"))  # noqa: S108
    state.ctx.branch = "chore/dep-update-20260610"
    state.ctx.worktree_path = Path("/tmp/r/.worktrees/chore-dep-update-20260610")  # noqa: S108
    return state


def test_run_updaters_commits_changed_only(monkeypatch) -> None:
    runs: list[tuple[str, ...]] = []
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: runs.append(a))
    state = _state()
    state.registry = [_Changed(), _NoChange()]
    run_updaters_stage(state)
    assert state.ctx.any_changes is True
    assert ("commit", "-m", "chore(deps): s") in runs
    assert sum(1 for r in runs if r[0] == "commit") == 1


def test_run_updaters_no_changes_sets_flag_false(monkeypatch) -> None:
    monkeypatch.setattr(_MOD + ".git.run", lambda *a: None)
    state = _state()
    state.registry = [_NoChange()]
    run_updaters_stage(state)
    assert state.ctx.any_changes is False


def test_validate_stage_skips_when_no_changes(monkeypatch) -> None:
    called = {"ran": False}
    monkeypatch.setattr(_MOD + ".validate.run_validation", lambda: called.update(ran=True))
    state = _state()
    state.ctx.any_changes = False
    validate_stage(state)
    assert called["ran"] is False


def test_validate_stage_aborts_on_red(monkeypatch) -> None:
    def _boom() -> None:
        raise UpdateDepsError(phase="validate", command="vrg-validate", message="Validation failed.")

    monkeypatch.setattr(_MOD + ".validate.run_validation", _boom)
    state = _state()
    state.ctx.any_changes = True
    with pytest.raises(UpdateDepsError, match="Validation failed"):
        validate_stage(state)


def test_finalize_removes_worktree_on_noop(monkeypatch) -> None:
    cleaned = {"done": False}
    monkeypatch.setattr(_MOD + ".pr.cleanup_worktree", lambda ctx: cleaned.update(done=True))
    state = _state()
    state.ctx.any_changes = False
    finalize_stage(state)
    assert cleaned["done"] is True


def test_build_stages_order_and_modes() -> None:
    stages = build_stages()
    assert [s.name for s in stages] == [
        "preflight", "update", "validate", "prepare-pr", "merge", "finalize",
    ]
    modes = {s.name: s.mode for s in stages}
    assert modes["validate"] == "fail_fast"
    assert modes["finalize"] == "fail_defer"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError: ...orchestrator`

- [ ] **Step 3: Write the module**

```python
# src/vergil_tooling/lib/update_deps/orchestrator.py
"""Declarative stage pipeline for vrg-update-deps."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from vergil_tooling.lib import git
from vergil_tooling.lib.progress import Stage
from vergil_tooling.lib.update_deps import pr, validate
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.preflight import preflight
from vergil_tooling.lib.update_deps.updater import Updater, applicable_updaters
from vergil_tooling.lib.update_deps.updaters.python_uv import PythonUvUpdater

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_REGISTRY: list[Updater] = [PythonUvUpdater()]


@dataclass
class UpdateDepsState:
    """Pipeline state; ``ctx`` is populated by the preflight stage."""

    repo_root: Path
    ctx: UpdateDepsContext | None = None
    registry: list[Updater] = field(default_factory=lambda: list(DEFAULT_REGISTRY))


def _require_ctx(state: UpdateDepsState) -> UpdateDepsContext:
    if state.ctx is None:
        raise UpdateDepsError(
            phase="update",
            command="update_deps",
            message="Pipeline context missing — preflight did not run.",
        )
    return state.ctx


def preflight_stage(state: UpdateDepsState) -> None:
    state.ctx = preflight(repo_root=state.repo_root)


def run_updaters_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    for updater in applicable_updaters(ctx, registry=state.registry):
        result = updater.apply(ctx)
        ctx.results.append(result)
        if result.changed:
            ctx.any_changes = True
            git.run("add", "-A")
            git.run("commit", "-m", result.commit_message)


def validate_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    if not ctx.any_changes:
        print("No dependency changes — skipping validation.")
        return
    validate.run_validation()


def prepare_pr_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    if not ctx.any_changes:
        return
    pr.prepare_pr(ctx)


def merge_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    if not ctx.any_changes:
        return
    pr.merge_pr(ctx)


def finalize_stage(state: UpdateDepsState) -> None:
    ctx = _require_ctx(state)
    if not ctx.any_changes:
        print("No updates found — removing worktree.")
    else:
        print(f"Dependency update merged: {ctx.pr_url}")
    pr.cleanup_worktree(ctx)


def build_stages() -> list[Stage]:
    """The vrg-update-deps pipeline, in execution order."""
    return [
        Stage("preflight", preflight_stage, mode="fail_fast"),
        Stage("update", run_updaters_stage, mode="fail_fast"),
        Stage("validate", validate_stage, mode="fail_fast"),
        Stage("prepare-pr", prepare_pr_stage, mode="fail_fast"),
        Stage("merge", merge_stage, mode="fail_fast"),
        Stage("finalize", finalize_stage, mode="fail_defer"),
    ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_update_deps_orchestrator.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/update_deps/orchestrator.py \
  tests/vergil_tooling/test_update_deps_orchestrator.py
vrg-git commit -m "feat(update-deps): add stage pipeline with no-op/abort and worktree cleanup"
```

---

### Task 9: CLI entry point, identity gate, console script

**Files:**
- Create: `src/vergil_tooling/bin/vrg_update_deps.py`
- Modify: `pyproject.toml` (add the console script near the other `vrg-*` entries)
- Test: `tests/vergil_tooling/test_vrg_update_deps.py`

The entry point captures the root via `git.repo_root()` **before** any stage
`chdir`s, refuses agent identities (human-only, like `vrg-release`), then drives
the pipeline through `progress.run_pipeline`.

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_vrg_update_deps.py
from __future__ import annotations

from pathlib import Path

from vergil_tooling.bin import vrg_update_deps

_MOD = "vergil_tooling.bin.vrg_update_deps"


def test_main_refuses_agent_identity(monkeypatch, capsys) -> None:
    monkeypatch.setattr(_MOD + ".identity_mode.is_human", lambda: False)
    rc = vrg_update_deps.main([])
    assert rc == 1
    assert "human" in capsys.readouterr().err.lower()


def test_main_runs_pipeline_for_human(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setattr(_MOD + ".identity_mode.is_human", lambda: True)
    monkeypatch.setattr(_MOD + ".git.repo_root", lambda: Path("/tmp/r"))  # noqa: S108
    monkeypatch.setattr(
        _MOD + ".progress.run_pipeline",
        lambda state, stages, **kw: seen.update(command=kw["command"], root=kw["repo_root"]) or 0,
    )
    rc = vrg_update_deps.main([])
    assert rc == 0
    assert seen["command"] == "vrg-update-deps"
    assert seen["root"] == Path("/tmp/r")  # noqa: S108
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_update_deps.py -v`
Expected: FAIL — `ModuleNotFoundError: ...vrg_update_deps`

- [ ] **Step 3: Write the entry point**

```python
# src/vergil_tooling/bin/vrg_update_deps.py
"""Mechanized dependency update — human-invoked, deterministic.

Runs on a clean, synced develop: upgrades dependencies in a managed worktree,
validates once, and drives the PR through merge and finalize. A no-op run
creates no PR.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, identity_mode, progress
from vergil_tooling.lib.update_deps.orchestrator import UpdateDepsState, build_stages


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the mechanized dependency-update workflow on develop.",
    )
    progress.add_progress_args(parser, build_stages())
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if not identity_mode.is_human():
        print(
            "vrg-update-deps is a human-only command (PR submission, merge, and "
            "finalization are human actions). Refusing to run as an agent.",
            file=sys.stderr,
        )
        return 1
    args = parse_args(argv)
    repo_root = git.repo_root()
    state = UpdateDepsState(repo_root=repo_root)
    return progress.run_pipeline(
        state,
        build_stages(),
        command="vrg-update-deps",
        label="vrg-update-deps",
        args=args,
        repo_root=repo_root,
    )


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Register the console script**

In `pyproject.toml`, under `[project.scripts]`, add the entry alphabetically
near the other `vrg-*` scripts:

```toml
vrg-update-deps = "vergil_tooling.bin.vrg_update_deps:main"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_update_deps.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Run the full new suite + validation**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/ -k update_deps -v`
Expected: PASS (all update_deps tests)

Run: `vrg-container-run -- vrg-validate`
Expected: PASS (lint, typecheck, tests, audit all green)

- [ ] **Step 7: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/bin/vrg_update_deps.py \
  pyproject.toml tests/vergil_tooling/test_vrg_update_deps.py
vrg-git commit -m "feat(update-deps): add vrg-update-deps CLI entry point and console script"
```

---

## Manual end-to-end check (after Task 9)

Python is the only locally-exercisable language here, so do one real dry run
from the repo root on a clean, synced `develop`:

1. Run `vrg-update-deps`.
2. If `uv lock --upgrade` produces no change, confirm it prints "No updates
   found", removes the worktree, and leaves **no** `.worktrees/chore-dep-update-*`
   dir, no branch, and no PR; the root is still on `develop`.
3. If it does change `uv.lock`, confirm it commits in the worktree, validates,
   opens a PR to develop, waits for CI, merges, then removes the worktree and
   branch.

This is a human verification step, not an automated test.

---

## Self-review

**Spec coverage (Phase 1 slice):**
- Generic driver / stage pipeline → Task 8.
- Managed-worktree mechanism (create off develop, chdir, remove on finalize, abort leaves it) → Task 4 + preflight (Task 5) + `cleanup_worktree` (Task 7) + `finalize_stage` (Task 8).
- Preconditions: on develop, clean tree, **in sync with origin/develop** → Task 5.
- Updater interface + registry → Task 2.
- Python/uv updater → Task 3.
- Single `vrg-validate`, all-or-nothing → Task 6 + `validate_stage` (Task 8).
- PR create/merge/finalize reusing release libs → Task 7.
- No-op (remove worktree, no PR) → `finalize_stage` no-change path (Task 8) + `cleanup_worktree` (Task 7), tested in Task 8.
- Abort leaves worktree → `validate` is `fail_fast`; pipeline stops before finalize. Tested via `test_validate_stage_aborts_on_red` (Task 8).
- Human-only identity gating → Task 9.
- Deferred items (config section, per-run tracking issue, vergil updater, other languages, actions, extensions, warn-only, `--with-deps`, vrg-release retrofit #1578) are listed under "Scope of this plan" and intentionally absent.

**Type consistency:** `UpdateDepsContext` fields (`repo`, `repo_root`, `branch`,
`worktree_path`, `pr_url`, `any_changes`, `results`) are used identically across
Tasks 1, 5, 7, 8. `UpdateResult` fields (`updater`, `changed`, `summary`,
`commit_message`, `warnings`) are consistent across Tasks 2, 3, 7, 8.
`create_worktree`/`remove_worktree` signatures (Task 4) match their callers in
preflight (Task 5) and `cleanup_worktree` (Task 7). Stage names in Task 8's
`build_stages` match the test assertions. Updaters never touch git — the
orchestrator commits (Task 8), preserving the contract from Task 2.

**Placeholder scan:** No `TBD`/`TODO`/"add error handling" placeholders; every
code step carries complete, runnable code. `github.create_pr` is called with
`body_file=str(body_path)` (the signature takes a `str`).
