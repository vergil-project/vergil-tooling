# Worktree Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `vrg-worktree-status` command that classifies every canonical `.worktrees/` worktree, and fix the `vrg-finalize-pr` straggler sweep so it stops orphaning squash-merged worktrees — both sharing one classification function.

**Architecture:** A pure `classify_worktree` function plus an I/O wrapper `gather_worktree_status` live in `lib/worktrees.py`. The new `vrg-worktree-status` bin renders them as a table; the finalize sweep consumes `gather_worktree_status` for a new "worktree arm" alongside the existing ancestry arm, so what status flags as cruft and what finalize removes are identical by construction.

**Tech Stack:** Python 3.14, argparse CLIs, `unittest.mock` + pytest, existing `lib/git.py` and `lib/github.py` subprocess wrappers.

---

## Execution environment

- **All work happens inside the worktree** `/.worktrees/issue-1552-worktree-status/` on branch `feature/1552-worktree-status`. Use absolute paths or `cd` into it first.
- **Run tests** through the dev container:
  `vrg-container-run -- uv run pytest <nodeid> -v`
- **Validate** (the only validation command):
  `vrg-container-run -- vrg-validate`
- **Commit** with `vrg-commit` (staging via `vrg-git add`). Never raw `git`/`gh`.

## File structure

- **Modify** `src/vergil_tooling/lib/git.py` — add `commits_ahead(base, branch)`.
- **Modify** `src/vergil_tooling/lib/worktrees.py` — add `WorktreeState`, `WorktreeStatus`, `classify_worktree` (pure), `_resolve_pr_state`, `gather_worktree_status` (I/O); import `github` + `subprocess` + `enum`.
- **Create** `src/vergil_tooling/bin/vrg_worktree_status.py` — the read-only CLI.
- **Modify** `pyproject.toml` — register the `vrg-worktree-status` console script.
- **Modify** `src/vergil_tooling/bin/vrg_finalize_pr.py` — split the straggler sweep into a worktree arm (new) + ancestry arm (existing).
- **Tests:** `tests/vergil_tooling/test_git.py`, `test_worktrees.py`, new `test_vrg_worktree_status.py`, `test_vrg_finalize_pr.py`.

---

## Task 1: `git.commits_ahead` helper

**Files:**
- Modify: `src/vergil_tooling/lib/git.py` (add after `merged_branches`, ~line 118)
- Test: `tests/vergil_tooling/test_git.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/vergil_tooling/test_git.py`:

```python
def test_commits_ahead_parses_rev_list_count() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="3") as ro:
        assert git.commits_ahead("develop", "feature/x") == 3
    ro.assert_called_once_with("rev-list", "--count", "develop..feature/x")
```

(If `test_git.py` does not already `from vergil_tooling.lib import git` and `from unittest.mock import patch`, add those imports.)

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_git.py::test_commits_ahead_parses_rev_list_count -v`
Expected: FAIL — `AttributeError: module 'vergil_tooling.lib.git' has no attribute 'commits_ahead'`

- [ ] **Step 3: Write minimal implementation**

Add to `src/vergil_tooling/lib/git.py` after `merged_branches`:

```python
def commits_ahead(base: str, branch: str) -> int:
    """Return the number of commits on *branch* not reachable from *base*."""
    return int(read_output("rev-list", "--count", f"{base}..{branch}"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_git.py::test_commits_ahead_parses_rev_list_count -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/git.py tests/vergil_tooling/test_git.py
vrg-commit --type feat --scope git --message "add commits_ahead helper (#1552)" \
  --body "Counts commits on a branch not reachable from a base via rev-list --count; used by worktree classification."
```

---

## Task 2: `WorktreeState` + `WorktreeStatus` + `classify_worktree` (pure)

**Files:**
- Modify: `src/vergil_tooling/lib/worktrees.py`
- Test: `tests/vergil_tooling/test_worktrees.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_worktrees.py`. First extend the import block:

```python
from vergil_tooling.lib.worktrees import (
    Worktree,
    WorktreeState,
    WorktreeStatus,
    classify_worktree,
    list_worktrees,
    require_tty,
    select_worktree,
    worktree_for_branch,
)
```

Then add:

```python
_SAMPLE_WT = Worktree(path=Path("/repo/.worktrees/issue-1-x"), branch="feature/1-x")


def _classify(**overrides: object) -> WorktreeStatus:
    base: dict[str, object] = {
        "pr_number": None,
        "pr_state": None,
        "pr_lookup_failed": False,
        "ahead": 0,
        "dirty": False,
    }
    base.update(overrides)
    return classify_worktree(_SAMPLE_WT, **base)  # type: ignore[arg-type]


def test_classify_open_pr_not_removable() -> None:
    status = _classify(pr_number=10, pr_state="OPEN", ahead=2)
    assert status.state is WorktreeState.OPEN_PR
    assert status.removable is False


def test_classify_merged_is_removable() -> None:
    status = _classify(pr_number=11, pr_state="MERGED", ahead=2)
    assert status.state is WorktreeState.MERGED
    assert status.removable is True


def test_classify_closed_is_removable() -> None:
    status = _classify(pr_number=12, pr_state="CLOSED", ahead=1)
    assert status.state is WorktreeState.CLOSED
    assert status.removable is True


def test_classify_no_pr_with_commits_is_stalled() -> None:
    status = _classify(ahead=1)
    assert status.state is WorktreeState.NO_PR
    assert status.removable is False


def test_classify_no_pr_zero_commits_is_draft() -> None:
    status = _classify(ahead=0)
    assert status.state is WorktreeState.DRAFT


def test_classify_dirty_merged_is_not_removable() -> None:
    status = _classify(pr_number=11, pr_state="MERGED", ahead=2, dirty=True)
    assert status.state is WorktreeState.MERGED
    assert status.dirty is True
    assert status.removable is False


def test_classify_lookup_failure_is_unknown() -> None:
    status = _classify(pr_lookup_failed=True, detail="gh exploded")
    assert status.state is WorktreeState.UNKNOWN
    assert status.removable is False
    assert status.detail == "gh exploded"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -k classify -v`
Expected: FAIL — `ImportError: cannot import name 'WorktreeState'`

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/lib/worktrees.py`, change the imports at the top from:

```python
from dataclasses import dataclass
from pathlib import Path

from vergil_tooling.lib import git
```

to:

```python
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from vergil_tooling.lib import git
```

Then add, immediately after the `Worktree` dataclass:

```python
class WorktreeState(str, Enum):
    """Lifecycle state of a canonical worktree, derived from PR + local signals."""

    OPEN_PR = "open-pr"
    NO_PR = "no-pr"
    DRAFT = "draft"
    MERGED = "merged"
    CLOSED = "closed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorktreeStatus:
    """A worktree's derived lifecycle state and the signals behind it."""

    worktree: Worktree
    state: WorktreeState
    pr_number: int | None
    ahead: int
    dirty: bool
    detail: str | None = None

    @property
    def removable(self) -> bool:
        """True when the worktree is finished cruft safe to delete.

        Merged or closed PRs are removable — unless the tree is dirty,
        in which case there is uncommitted work to rescue first.
        """
        return self.state in (WorktreeState.MERGED, WorktreeState.CLOSED) and not self.dirty


def classify_worktree(
    worktree: Worktree,
    *,
    pr_number: int | None,
    pr_state: str | None,
    pr_lookup_failed: bool,
    ahead: int,
    dirty: bool,
    detail: str | None = None,
) -> WorktreeStatus:
    """Map already-gathered signals to a WorktreeStatus. Pure: no I/O.

    A failed PR lookup yields UNKNOWN with *detail* — never a silent
    downgrade to NO_PR, which would mislabel real work as stalled.
    """
    if pr_lookup_failed:
        state = WorktreeState.UNKNOWN
    elif pr_state == "OPEN":
        state = WorktreeState.OPEN_PR
    elif pr_state == "MERGED":
        state = WorktreeState.MERGED
    elif pr_state == "CLOSED":
        state = WorktreeState.CLOSED
    elif ahead > 0:
        state = WorktreeState.NO_PR
    else:
        state = WorktreeState.DRAFT
    return WorktreeStatus(
        worktree=worktree,
        state=state,
        pr_number=pr_number,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -k classify -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/worktrees.py tests/vergil_tooling/test_worktrees.py
vrg-commit --type feat --scope worktrees \
  --message "add classify_worktree lifecycle classifier (#1552)" \
  --body "Pure function mapping PR state plus local signals to a WorktreeState; removable verdict covers merged/closed minus a dirty overlay. Failed lookups surface as UNKNOWN, never a silent NO_PR."
```

---

## Task 3: `gather_worktree_status` (I/O wrapper)

**Files:**
- Modify: `src/vergil_tooling/lib/worktrees.py`
- Test: `tests/vergil_tooling/test_worktrees.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/vergil_tooling/test_worktrees.py` (extend the `worktrees` import to also include `gather_worktree_status`, and add `import subprocess` at the top):

```python
def test_gather_open_pr_short_circuits_closed_lookup() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=2),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(
            _MOD + ".github.pr_for_branch",
            return_value={"number": "10", "url": "", "title": "t"},
        ),
        patch(_MOD + ".github.closed_pr_for_branch") as closed,
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.OPEN_PR
    assert status.pr_number == 10
    closed.assert_not_called()


def test_gather_merged_pr_is_removable() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.MERGED
    assert status.pr_number == 11
    assert status.removable is True


def test_gather_dirty_overlay_blocks_removal() -> None:
    with (
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".git.read_output", return_value=" M file.py"),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(
            _MOD + ".github.closed_pr_for_branch",
            return_value={"number": "11", "url": "", "title": "t"},
        ),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.dirty is True
    assert status.removable is False


def test_gather_pr_lookup_failure_is_unknown() -> None:
    err = subprocess.CalledProcessError(1, ["gh"], stderr="boom")
    with (
        patch(_MOD + ".git.commits_ahead", return_value=0),
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".github.pr_for_branch", side_effect=err),
    ):
        status = gather_worktree_status(_SAMPLE_WT, target="develop")
    assert status.state is WorktreeState.UNKNOWN
    assert "boom" in (status.detail or "")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -k gather -v`
Expected: FAIL — `ImportError: cannot import name 'gather_worktree_status'`

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/lib/worktrees.py`, add `subprocess` to the top imports and add `github` to the lib import:

```python
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from vergil_tooling.lib import git, github
from vergil_tooling.lib.repo_init import prompt_choice
```

Then add, after `classify_worktree`:

```python
def _resolve_pr_state(branch: str) -> tuple[int | None, str | None]:
    """Resolve ``(pr_number, pr_state)`` for *branch*.

    An open PR wins; otherwise the most recent closed/merged PR (whose
    ``MERGED`` vs ``CLOSED`` state is read explicitly); otherwise
    ``(None, None)`` for a branch with no PR.
    """
    open_pr = github.pr_for_branch(branch)
    if open_pr is not None:
        return int(open_pr["number"]), "OPEN"
    closed = github.closed_pr_for_branch(branch)
    if closed is not None:
        return int(closed["number"]), github.pr_state(closed["number"])
    return None, None


def gather_worktree_status(worktree: Worktree, *, target: str) -> WorktreeStatus:
    """Gather local + remote signals for *worktree* and classify it.

    The single source of truth shared by ``vrg-worktree-status`` and the
    ``vrg-finalize-pr`` straggler sweep. A failed ``gh`` PR lookup is
    surfaced as ``UNKNOWN`` with the captured reason — never a silent
    failure that would misclassify the worktree.
    """
    ahead = git.commits_ahead(target, worktree.branch)
    dirty = bool(git.read_output("-C", str(worktree.path), "status", "--porcelain"))
    try:
        pr_number, pr_state = _resolve_pr_state(worktree.branch)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or str(exc)).strip()
        return classify_worktree(
            worktree,
            pr_number=None,
            pr_state=None,
            pr_lookup_failed=True,
            ahead=ahead,
            dirty=dirty,
            detail=detail,
        )
    return classify_worktree(
        worktree,
        pr_number=pr_number,
        pr_state=pr_state,
        pr_lookup_failed=False,
        ahead=ahead,
        dirty=dirty,
    )
```

Note: `sys` is already imported in this module (used by `require_tty`); keep it. The example import block above shows the final ordering.

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -k gather -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Run the whole worktrees test file (regression)**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -v`
Expected: PASS (all existing + new)

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/lib/worktrees.py tests/vergil_tooling/test_worktrees.py
vrg-commit --type feat --scope worktrees \
  --message "add gather_worktree_status I/O wrapper (#1552)" \
  --body "Gathers commits-ahead, dirty, and PR state per worktree then classifies. Open PR short-circuits the closed lookup; a failed gh lookup yields UNKNOWN with the reason."
```

---

## Task 4: `vrg-worktree-status` command

**Files:**
- Create: `src/vergil_tooling/bin/vrg_worktree_status.py`
- Modify: `pyproject.toml` (`[project.scripts]`)
- Test: `tests/vergil_tooling/test_vrg_worktree_status.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/vergil_tooling/test_vrg_worktree_status.py`:

```python
"""Tests for vergil_tooling.bin.vrg_worktree_status."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_worktree_status import main
from vergil_tooling.lib.worktrees import Worktree, WorktreeState, WorktreeStatus

_MOD = "vergil_tooling.bin.vrg_worktree_status"


def _status(
    branch: str,
    state: WorktreeState,
    *,
    pr: int | None = None,
    ahead: int = 0,
    dirty: bool = False,
    detail: str | None = None,
) -> WorktreeStatus:
    wt = Worktree(path=Path(f"/repo/.worktrees/{branch.replace('/', '-')}"), branch=branch)
    return WorktreeStatus(
        worktree=wt, state=state, pr_number=pr, ahead=ahead, dirty=dirty, detail=detail
    )


def test_main_groups_cruft_last_and_summarizes(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [
        _status("feature/1470-merged", WorktreeState.MERGED, pr=1471, ahead=2),
        _status("feature/1534-open", WorktreeState.OPEN_PR, pr=1544, ahead=2),
        _status("feature/1543-nopr", WorktreeState.NO_PR, ahead=1),
    ]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.index("open-pr") < out.index("merged")
    assert out.index("no-pr") < out.index("merged")
    assert "1 active" in out
    assert "1 stalled (no-pr)" in out
    assert "1 cruft (removable)" in out
    assert "Run vrg-finalize-pr to clean cruft." in out


def test_main_empty_reports_none(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
    ):
        rc = main([])
    assert rc == 0
    assert "No canonical" in capsys.readouterr().out


def test_main_surfaces_unknown_detail(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/9-x", WorktreeState.UNKNOWN, detail="gh boom")]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "gh boom" in out
    assert "0 cruft" in out
    assert "Run vrg-finalize-pr" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_worktree_status.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.bin.vrg_worktree_status'`

- [ ] **Step 3: Write the implementation**

Create `src/vergil_tooling/bin/vrg_worktree_status.py`:

```python
"""List canonical ``.worktrees/`` worktrees with their lifecycle state.

Read-only observability for worktree hygiene: shows which worktrees are
removable cruft (merged/closed PRs whose worktree was never cleaned up)
versus legitimate in-flight work, so the cruft is obvious at a glance.

Cleanup stays ``vrg-finalize-pr``'s job — this command only observes
(issue #1552). PR state is queried from GitHub (one call per worktree)
for an authoritative merged/closed verdict; a failed lookup is shown as
``unknown`` with the reason rather than silently downgraded.
"""

from __future__ import annotations

import argparse
import sys

from vergil_tooling.lib import git, worktrees
from vergil_tooling.lib.worktrees import WorktreeState, WorktreeStatus

# Live work first, cruft last, so the removable rows group at the bottom.
_SORT_RANK = {
    WorktreeState.OPEN_PR: 0,
    WorktreeState.NO_PR: 1,
    WorktreeState.DRAFT: 2,
    WorktreeState.UNKNOWN: 3,
    WorktreeState.MERGED: 4,
    WorktreeState.CLOSED: 5,
}

_COLUMNS = ("WORKTREE", "BRANCH", "PR", "STATE", "AHEAD", "DIRTY")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="List canonical worktrees with their lifecycle state.",
    )
    parser.add_argument(
        "--target-branch",
        default="develop",
        help="Branch to measure commits-ahead against (default: develop).",
    )
    return parser.parse_args(argv)


def _row(status: WorktreeStatus) -> tuple[str, ...]:
    pr = f"#{status.pr_number}" if status.pr_number is not None else "-"
    return (
        status.worktree.path.name,
        status.worktree.branch,
        pr,
        status.state.value,
        str(status.ahead),
        "yes" if status.dirty else "-",
    )


def _render_table(rows: list[tuple[str, ...]]) -> str:
    cells = [_COLUMNS, *rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(_COLUMNS))]
    return "\n".join(
        "  ".join(row[i].ljust(widths[i]) for i in range(len(_COLUMNS))).rstrip() for row in cells
    )


def _summary(statuses: list[WorktreeStatus]) -> str:
    total = len(statuses)
    cruft = sum(1 for s in statuses if s.removable)
    stalled = sum(1 for s in statuses if s.state is WorktreeState.NO_PR)
    active = total - cruft - stalled
    line = (
        f"{total} worktrees — {active} active, "
        f"{stalled} stalled (no-pr), {cruft} cruft (removable)."
    )
    if cruft:
        line += " Run vrg-finalize-pr to clean cruft."
    return line


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    root = git.repo_root()
    statuses = [
        worktrees.gather_worktree_status(wt, target=args.target_branch)
        for wt in worktrees.list_worktrees(root)
    ]
    if not statuses:
        print("No canonical .worktrees/ worktrees found.")
        return 0
    statuses.sort(key=lambda s: (_SORT_RANK[s.state], s.worktree.branch))
    print(_render_table([_row(s) for s in statuses]))
    print()
    print(_summary(statuses))
    # Surface UNKNOWN detail so a failed lookup is never silently hidden.
    for status in statuses:
        if status.state is WorktreeState.UNKNOWN and status.detail:
            print(f"  note: {status.worktree.branch}: {status.detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Register the console script**

In `pyproject.toml`, under `[project.scripts]`, add this line in alphabetical position (after `vrg-whoami` / `vrg-wait-until-green` — keep the block's existing ordering):

```toml
vrg-worktree-status = "vergil_tooling.bin.vrg_worktree_status:main"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_worktree_status.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_worktree_status.py pyproject.toml tests/vergil_tooling/test_vrg_worktree_status.py
vrg-commit --type feat --scope worktree-status \
  --message "add vrg-worktree-status command (#1552)" \
  --body "Read-only command listing canonical worktrees with their lifecycle state; cruft groups at the bottom and a summary counts active/stalled/cruft. Cleanup stays vrg-finalize-pr's job."
```

---

## Task 5: squash-merge-aware finalize straggler sweep

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py` (`_stage_cleanup`, the sweep block ~lines 372-404)
- Test: `tests/vergil_tooling/test_vrg_finalize_pr.py`

**Context:** Existing sweep tests drive the *ancestry arm* by patching `git.merged_branches` while the autouse fixture leaves `worktrees.list_worktrees` returning `[]`. Those tests stay valid — the ancestry arm is preserved. This task adds the *worktree arm* and two tests pinning it.

- [ ] **Step 1: Write the failing tests**

In `tests/vergil_tooling/test_vrg_finalize_pr.py`, extend the worktrees import:

```python
from vergil_tooling.lib.worktrees import Worktree, WorktreeState, WorktreeStatus
```

Then add:

```python
def test_sweep_removes_squash_merged_worktree_branch(tmp_path: Path) -> None:
    """Issue #1552: a squash-merged branch is invisible to
    `git branch --merged`, so the worktree arm must sweep it via its
    classify_worktree removable verdict."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-1-x", branch="feature/1-x")
    removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.MERGED, pr_number=1, ahead=2, dirty=False
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=removable),
        patch(_MOD + "._delete_branch_and_worktree", return_value=True) as deleter,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_any_call("feature/1-x", tmp_path, dry_run=False)


def test_sweep_keeps_open_pr_worktree(tmp_path: Path) -> None:
    """Race-safety (issue #1445): a worktree whose PR is still open is
    not removable, so the worktree arm never deletes live work."""
    _make_profile(tmp_path, "library-release")
    wt = Worktree(path=tmp_path / ".worktrees" / "issue-2-y", branch="feature/2-y")
    not_removable = WorktreeStatus(
        worktree=wt, state=WorktreeState.OPEN_PR, pr_number=2, ahead=2, dirty=False
    )
    with (
        patch(_MOD + ".git.repo_root", return_value=tmp_path),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".worktrees.gather_worktree_status", return_value=not_removable),
        patch(_MOD + "._delete_branch_and_worktree") as deleter,
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
    ):
        result = main(["--cleanup-only"])
    assert result == 0
    deleter.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -k "squash_merged_worktree or open_pr_worktree" -v`
Expected: FAIL — `test_sweep_removes_squash_merged_worktree_branch` fails its `assert_any_call` (the worktree arm doesn't exist yet, so the squash-merged branch is never swept).

- [ ] **Step 3: Write the implementation**

In `src/vergil_tooling/bin/vrg_finalize_pr.py`, replace the existing ancestry-sweep block in `_stage_cleanup` (from the `print("Checking for merged local branches...")` line through the end of the `for branch in git.merged_branches(...)` loop) with:

```python
    # Straggler sweep. Candidates come from two sources, because a squash
    # merge rewrites the branch's work onto the target as a new commit:
    # the branch tip is never an ancestor, so `git branch --merged` (the
    # ancestry arm) cannot see squash-merged branches (issue #1552). The
    # worktree arm closes that gap by classifying every canonical worktree
    # with the same logic that backs vrg-worktree-status, so "cruft" and
    # "removed" match by construction.
    print("Checking for merged local branches...")
    worktree_by_branch = {wt.branch: wt for wt in worktrees.list_worktrees(root)}

    # Worktree arm (squash-merge-aware). classify_worktree's removable
    # verdict (MERGED/CLOSED and not dirty) replaces the ancestry guards
    # here and is race-safe against parallel agents (issue #1445): a branch
    # with no merged/closed PR — including a freshly created one — is never
    # removable.
    for branch, wt in worktree_by_branch.items():
        if branch in eternal or branch in deleted:
            continue
        status = worktrees.gather_worktree_status(wt, target=args.target_branch)
        if not status.removable:
            print(f"  Skipping {branch}: {status.state.value} (not removable)")
            continue
        if _delete_branch_and_worktree(branch, root, dry_run=args.dry_run):
            deleted.append(branch)

    # Ancestry arm. Catches branches with no canonical worktree (worktree
    # already gone, or merge-commit/rebase merges whose tip IS an ancestor).
    # `git branch --merged` classifies a branch as merged when its tip is an
    # ancestor of the target — which a branch just created from the target's
    # tip satisfies trivially, so the two guards below (issue #1445) gate the
    # removal as strictly as the worktree arm above.
    for branch in git.merged_branches(args.target_branch):
        if branch in eternal or branch in deleted or branch in worktree_by_branch:
            continue
        # Guard 1 — skip zero-commit branches. A tip equal to the target's
        # carries no merged work; it is also what an in-flight branch looks
        # like before its first commit.
        if git.commit_sha(branch) == git.commit_sha(args.target_branch):
            print(
                f"  Skipping {branch}: tip matches {args.target_branch} "
                "(zero-commit branch, nothing to clean up)"
            )
            continue
        # Guard 2 — require merge evidence: only sweep branches whose head
        # has a closed or merged PR.
        if github.closed_pr_for_branch(branch) is None:
            print(f"  Skipping {branch}: no closed or merged PR for this branch")
            continue
        if _delete_branch_and_worktree(branch, root, dry_run=args.dry_run):
            deleted.append(branch)
```

(The explicit-target step above this block — handling `ctx.merged_branch` — is unchanged. The `deleted` list is still defined just above it, so both arms append to it.)

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -k "squash_merged_worktree or open_pr_worktree" -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the whole finalize test file (regression — ancestry arm preserved)**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v`
Expected: PASS (all existing ancestry-arm tests + the 2 new worktree-arm tests)

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type fix --scope finalize-pr \
  --message "sweep squash-merged worktrees via classify_worktree (#1552)" \
  --body "The straggler sweep relied on git branch --merged, which is blind to squash-merged branches and orphaned their worktrees. Add a worktree arm that classifies every canonical worktree with gather_worktree_status and removes the removable ones; the ancestry arm still handles worktree-less branches. Removal set now matches vrg-worktree-status's cruft verdict by construction."
```

---

## Task 6: full validation + docs touch-up

**Files:**
- Possibly modify: a docs/CLI reference if one enumerates `vrg-*` commands (check `docs/` and the project `CLAUDE.md` command list — update only if such a list exists).

- [ ] **Step 1: Check for a command index to update**

Run: `grep -rl "vrg-finalize-pr" docs/ README.md 2>/dev/null`
If a file lists the `vrg-*` commands (e.g. a reference table), add a one-line entry for `vrg-worktree-status` ("List canonical worktrees with their lifecycle state"). If no such index exists, skip — do not invent docs.

- [ ] **Step 2: Run full validation**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS (lint, typecheck, tests, audit, common checks).

- [ ] **Step 3: Fix anything validation flags, then re-run until green.**

- [ ] **Step 4: Commit any doc/validation fixups**

```bash
vrg-git add -A
vrg-commit --type docs --scope worktree-status \
  --message "document vrg-worktree-status in command index (#1552)" \
  --body "Add the new command to the CLI reference."
```

(Skip this commit if Steps 1 and 3 produced no changes.)

---

## Manual integration test (after merge)

Not an automated step — the live validation of the fix. After this work merges and the new tooling deploys:

1. Run `vrg-worktree-status` — the 7 known orphans (issues 1470, 1492, 1499, 1501, 1519, 1520, 1523) should each show `merged` / cruft; the live worktrees (1534, 1547, 1543) should show `open-pr` / `open-pr` / `no-pr`.
2. Run `vrg-finalize-pr` (cleanup path) — it should remove exactly the 7 merged worktrees and leave the 3 live ones untouched.

---

## Self-review notes

- **Spec coverage:** status command (Tasks 2-4), squash-merge sweep fix (Task 5), shared `classify_worktree`/`gather_worktree_status` source of truth (Tasks 2-3, consumed in 4 and 5), no-silent-failure UNKNOWN path (Tasks 2-4), read-only/exit-0/no-prune (Task 4), `commits_ahead` signal (Task 1). All spec sections map to a task.
- **Type consistency:** `WorktreeState`, `WorktreeStatus(worktree, state, pr_number, ahead, dirty, detail)`, `classify_worktree(...)`, `gather_worktree_status(worktree, *, target)`, `git.commits_ahead(base, branch)` are used identically across tasks.
- **No placeholders:** every code and test step shows complete content.
