# PR Interface Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vrg-submit-pr` runnable from the repo root (worktree-aware) and give
`vrg-finalize-pr` PR inference, always-confirm behavior, and a fail-fast wait-for-green
merge loop shared with the release workflow.

**Architecture:** Two new shared modules — `lib/worktrees.py` (canonical `.worktrees/`
discovery + selection) and `lib/pr_merge.py` (fail-fast wait-and-merge engine) — consumed
by both CLI tools. `lib/release/merge.py` becomes a thin wrapper over the engine.
`bin/vrg_submit_pr.py` gains a location-resolution preamble; `bin/vrg_finalize_pr.py`
gains PR inference, confirmation prompts, and explicit-target cleanup.

**Tech Stack:** Python 3.12, argparse, pytest + unittest.mock, gh CLI via `lib/github.py`.

**Issue:** vergil-project/vergil-tooling#1423
**Spec:** `docs/specs/2026-06-05-pr-interface-design.md` (status: approved + pushback-reviewed — read it first)

---

## Repo ground rules (read before Task 1)

- Work **only** inside the worktree `.worktrees/issue-1423-pr-interface/` on branch
  `feature/1423-pr-interface`. `cd` there for every Bash command. The spec is already
  committed on this branch.
- Use `vrg-git` (never `git`) and `vrg-commit` (never raw commit). `vrg-commit` usage:
  `vrg-commit --type <type> --scope <scope> --message "<desc>" [--body "<body>"]`
  It commits whatever is staged — stage with `vrg-git add <paths>` first.
- Heredocs (`<<EOF`) are blocked. Pass multi-line content via files (Write tool).
- **Hook caveat:** multi-line `--body` strings must not contain lines that *start with*
  a guarded tool name (`git`, `gh`, `vrg-submit-pr`, …) — a known hook false-positive
  (vergil-claude-plugin#450) blocks the command. Reword or indent such lines.
- Tests run inside the dev container: `vrg-container-run -- uv run pytest <args>`.
  Final validation: `vrg-container-run -- uv run vrg-validate` (the only full-validation
  command; never run linters individually).
- Style: `from __future__ import annotations`, full type hints, double quotes, ruff
  line-length 100, py312. Subprocess calls carry `# noqa: S603` / `# noqa: S607` —
  copy the patterns in existing code.
- Mock at the module-under-test namespace, e.g.
  `patch("vergil_tooling.bin.vrg_finalize_pr.github.merge")` — see
  `tests/vergil_tooling/test_vrg_finalize_pr.py` for the house style.

## File structure

| File | Action | Responsibility |
|---|---|---|
| `src/vergil_tooling/lib/worktrees.py` | Create | Canonical `.worktrees/` discovery, branch lookup, TTY guard, selection menu |
| `src/vergil_tooling/lib/pr_merge.py` | Create | Fail-fast wait-and-merge engine (`MergeAbort`, `wait_and_merge`) |
| `src/vergil_tooling/lib/github.py` | Modify | Add `pr_for_branch`, `is_draft`, `head_ref` |
| `src/vergil_tooling/lib/release/merge.py` | Modify | Thin wrapper over `pr_merge.wait_and_merge` |
| `src/vergil_tooling/bin/vrg_submit_pr.py` | Modify | Location-resolution preamble in template mode |
| `src/vergil_tooling/bin/vrg_finalize_pr.py` | Modify | PR inference, confirmations, engine swap, explicit-target cleanup |
| `tests/vergil_tooling/test_worktrees.py` | Create | Unit tests for `lib/worktrees.py` |
| `tests/vergil_tooling/test_pr_merge.py` | Create | Loop state-table tests |
| `tests/vergil_tooling/test_github.py` | Modify | Tests for the three new helpers |
| `tests/vergil_tooling/test_release_merge.py` | Modify | Wrapper regression tests |
| `tests/vergil_tooling/test_vrg_submit_pr.py` | Modify | Location matrix tests |
| `tests/vergil_tooling/test_vrg_finalize_pr.py` | Modify | Inference matrix + cleanup tests |
| `docs/site/docs/reference/dev/submit-pr.md` | Modify | Document root-launch behavior |
| `docs/site/docs/reference/dev/finalize-pr.md` | Modify | Document inference, wait, confirmations |

---

### Task 1: `lib/worktrees.py` — discovery, TTY guard, selection

**Files:**
- Create: `src/vergil_tooling/lib/worktrees.py`
- Test: `tests/vergil_tooling/test_worktrees.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/vergil_tooling/test_worktrees.py`:

```python
"""Tests for vergil_tooling.lib.worktrees."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.worktrees import (
    Worktree,
    list_worktrees,
    require_tty,
    select_worktree,
    worktree_for_branch,
)

_MOD = "vergil_tooling.lib.worktrees"

_PORCELAIN = """\
worktree /repo
HEAD 1111111111111111111111111111111111111111
branch refs/heads/develop

worktree /repo/.worktrees/issue-7-foo
HEAD 2222222222222222222222222222222222222222
branch refs/heads/feature/7-foo

worktree /elsewhere/rogue
HEAD 3333333333333333333333333333333333333333
branch refs/heads/feature/9-rogue

worktree /repo/.worktrees/issue-8-bar
HEAD 4444444444444444444444444444444444444444
branch refs/heads/feature/8-bar
"""


def test_list_worktrees_filters_to_canonical_container() -> None:
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN):
        result = list_worktrees(Path("/repo"))
    assert result == [
        Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo"),
        Worktree(path=Path("/repo/.worktrees/issue-8-bar"), branch="feature/8-bar"),
    ]


def test_list_worktrees_ignores_detached_worktrees() -> None:
    porcelain = "worktree /repo/.worktrees/issue-5-x\nHEAD 5555\ndetached\n"
    with patch(_MOD + ".git.read_output", return_value=porcelain):
        assert list_worktrees(Path("/repo")) == []


def test_worktree_for_branch_found() -> None:
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN):
        path = worktree_for_branch("feature/8-bar", Path("/repo"))
    assert path == Path("/repo/.worktrees/issue-8-bar")


def test_worktree_for_branch_ignores_non_canonical() -> None:
    with patch(_MOD + ".git.read_output", return_value=_PORCELAIN):
        assert worktree_for_branch("feature/9-rogue", Path("/repo")) is None


def test_require_tty_passes_on_tty() -> None:
    with patch(_MOD + ".sys.stdin") as stdin:
        stdin.isatty.return_value = True
        require_tty("test context")  # no raise


def test_require_tty_fails_fast_on_non_tty() -> None:
    with patch(_MOD + ".sys.stdin") as stdin:
        stdin.isatty.return_value = False
        with pytest.raises(SystemExit, match="interactive terminal"):
            require_tty("test context")


def test_select_worktree_single_candidate_no_prompt() -> None:
    wt = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
    with patch(_MOD + ".prompt_choice") as choice:
        result = select_worktree([wt], purpose="Pick one", labels=["foo"])
    assert result is wt
    choice.assert_not_called()


def test_select_worktree_multiple_candidates_prompts() -> None:
    wts = [
        Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo"),
        Worktree(path=Path("/repo/.worktrees/issue-8-bar"), branch="feature/8-bar"),
    ]
    with (
        patch(_MOD + ".require_tty"),
        patch(_MOD + ".prompt_choice", return_value="bar label") as choice,
    ):
        result = select_worktree(wts, purpose="Pick one", labels=["foo label", "bar label"])
    assert result is wts[1]
    choice.assert_called_once_with("Pick one", ["foo label", "bar label"])


def test_select_worktree_empty_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        select_worktree([], purpose="Pick one", labels=[])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.worktrees'`

- [ ] **Step 3: Write the implementation**

Create `src/vergil_tooling/lib/worktrees.py`:

```python
"""Discover and select canonical ``.worktrees/`` worktrees.

Single home for worktree-convention logic: enumeration of worktrees
under the canonical ``.worktrees/`` container, branch lookup, and
interactive selection. Worktrees elsewhere (developer-managed,
outside the convention) are deliberately ignored — auto-acting on
them would surprise the user. Issue #315.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from vergil_tooling.lib import git
from vergil_tooling.lib.repo_init import prompt_choice


@dataclass(frozen=True)
class Worktree:
    """A canonical worktree and the branch it has checked out."""

    path: Path
    branch: str


def list_worktrees(repo_root: Path) -> list[Worktree]:
    """Return worktrees under ``repo_root/.worktrees/`` with their branches.

    Detached worktrees (no ``branch`` line in the porcelain output) and
    worktrees outside the canonical container are excluded.
    """
    output = git.read_output("worktree", "list", "--porcelain")
    canonical_root = (repo_root / ".worktrees").resolve()

    worktrees: list[Worktree] = []
    current_path: Path | None = None
    for line in output.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.removeprefix("worktree ").strip())
        elif line.startswith("branch ") and current_path is not None:
            ref = line.removeprefix("branch ").strip()
            resolved = current_path.resolve()
            current_path = None
            try:
                resolved.relative_to(canonical_root)
            except ValueError:
                continue
            worktrees.append(
                Worktree(path=resolved, branch=ref.removeprefix("refs/heads/"))
            )
    return worktrees


def worktree_for_branch(branch: str, repo_root: Path) -> Path | None:
    """Return the canonical worktree path that has *branch* checked out, or None."""
    for wt in list_worktrees(repo_root):
        if wt.branch == branch:
            return wt.path
    return None


def require_tty(context: str) -> None:
    """Fail fast when an interactive prompt would read from non-TTY stdin.

    These tools are human touch points by design: a human is assumed to
    be present, and EOF-as-default would be a silent failure. Scripted
    use is served by explicit arguments, not by piping into prompts.
    """
    if not sys.stdin.isatty():
        msg = (
            f"{context} requires an interactive terminal.\n"
            "  Pass the target explicitly to run non-interactively."
        )
        raise SystemExit(msg)


def select_worktree(
    candidates: list[Worktree],
    *,
    purpose: str,
    labels: list[str],
) -> Worktree:
    """Choose among candidate worktrees; prompt only when there are several.

    ``labels`` must parallel ``candidates`` one-to-one and is what the
    menu displays.
    """
    if not candidates:
        msg = "select_worktree requires at least one candidate"
        raise ValueError(msg)
    if len(candidates) == 1:
        return candidates[0]
    require_tty(purpose)
    chosen = prompt_choice(purpose, labels)
    return candidates[labels.index(chosen)]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/worktrees.py tests/vergil_tooling/test_worktrees.py
vrg-commit --type feat --scope worktrees --message "add canonical worktree discovery and selection library (#1423)"
```

---

### Task 2: finalize-pr consumes `worktrees.worktree_for_branch`

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py` (delete `_worktree_for_branch`, lines ~84–112)
- Modify: `tests/vergil_tooling/test_vrg_finalize_pr.py` (retarget its tests)

- [ ] **Step 1: Retarget the existing `_worktree_for_branch` tests**

In `tests/vergil_tooling/test_vrg_finalize_pr.py`: the import block currently imports
`_worktree_for_branch` from `vergil_tooling.bin.vrg_finalize_pr`. Delete that name from
the import. Then delete the `_worktree_for_branch` test functions in this file — the
behavior is covered by `test_worktrees.py` from Task 1 (verify each deleted case has an
equivalent there; `test_worktree_for_branch_found` and
`test_worktree_for_branch_ignores_non_canonical` are the required pair — if the old file
tests a case Task 1 missed, port it to `test_worktrees.py` instead of deleting it).

- [ ] **Step 2: Replace the private helper with the library call**

In `src/vergil_tooling/bin/vrg_finalize_pr.py`:

1. Add to the imports: `from vergil_tooling.lib import config, git, github, pr_provenance, worktrees`
   (extend the existing `from vergil_tooling.lib import ...` line).
2. Delete the entire `_worktree_for_branch` function (docstring included).
3. In the cleanup loop, change:

```python
        wt = _worktree_for_branch(branch, root)
```

to:

```python
        wt = worktrees.worktree_for_branch(branch, root)
```

- [ ] **Step 3: Run the finalize tests**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py tests/vergil_tooling/test_worktrees.py -v`
Expected: PASS. If a finalize test fails on a now-missing patch target
(`_MOD + "._worktree_for_branch"`), repoint that patch to `_MOD + ".worktrees.worktree_for_branch"`.

- [ ] **Step 4: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type refactor --scope finalize --message "use shared worktree discovery library (#1423)"
```

---

### Task 3: github helpers — `pr_for_branch`, `is_draft`, `head_ref`

**Files:**
- Modify: `src/vergil_tooling/lib/github.py` (add after `pr_state`, ~line 570)
- Test: `tests/vergil_tooling/test_github.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/vergil_tooling/test_github.py` (match the file's existing import/patch
style — it patches `vergil_tooling.lib.github.read_output` / `read_json`):

```python
def test_pr_for_branch_returns_first_open_pr() -> None:
    payload = [{"number": 1423, "url": "https://github.com/o/r/pull/1423", "title": "T"}]
    with patch("vergil_tooling.lib.github.read_json", return_value=payload) as rj:
        result = github.pr_for_branch("feature/1423-pr-interface")
    assert result == {
        "number": "1423",
        "url": "https://github.com/o/r/pull/1423",
        "title": "T",
    }
    rj.assert_called_once_with(
        "pr", "list", "--head", "feature/1423-pr-interface",
        "--state", "open", "--json", "number,url,title",
    )


def test_pr_for_branch_none_when_no_open_pr() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value=[]):
        assert github.pr_for_branch("feature/1423-pr-interface") is None


def test_is_draft_true() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="true"):
        assert github.is_draft("1423") is True


def test_is_draft_false() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="false"):
        assert github.is_draft("1423") is False


def test_head_ref() -> None:
    with patch("vergil_tooling.lib.github.read_output", return_value="feature/1423-x"):
        assert github.head_ref("1423") == "feature/1423-x"
```

(If the file imports github functions individually rather than as a module, follow its
existing convention and adjust the assertions accordingly.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github.py -v -k "pr_for_branch or is_draft or head_ref"`
Expected: FAIL — `AttributeError: ... has no attribute 'pr_for_branch'`

- [ ] **Step 3: Write the implementation**

Add to `src/vergil_tooling/lib/github.py`, directly after `pr_state` (~line 570):

```python
def pr_for_branch(branch: str) -> dict[str, str] | None:
    """Return the open PR whose head is *branch*, or None.

    GitHub permits at most one open PR per head/base pair within a
    repo, so taking the first result is safe for the same-repo
    workflow this serves.
    """
    result = read_json(
        "pr", "list", "--head", branch, "--state", "open", "--json", "number,url,title"
    )
    if not isinstance(result, list) or not result:
        return None
    first = result[0]
    if not isinstance(first, dict):
        return None
    return {
        "number": str(first.get("number", "")),
        "url": str(first.get("url", "")),
        "title": str(first.get("title", "")),
    }


def is_draft(pr: str) -> bool:
    """Return True if *pr* is a draft."""
    return read_output("pr", "view", pr, "--json", "isDraft", "--jq", ".isDraft") == "true"


def head_ref(pr: str) -> str:
    """Return the PR's head branch name."""
    return read_output("pr", "view", pr, "--json", "headRefName", "--jq", ".headRefName")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_github.py -v`
Expected: PASS (all, including pre-existing tests)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/github.py tests/vergil_tooling/test_github.py
vrg-commit --type feat --scope github --message "add pr_for_branch, is_draft, and head_ref helpers (#1423)"
```

---

### Task 4: `lib/pr_merge.py` — the fail-fast wait-and-merge engine

**Files:**
- Create: `src/vergil_tooling/lib/pr_merge.py`
- Test: `tests/vergil_tooling/test_pr_merge.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/vergil_tooling/test_pr_merge.py`:

```python
"""Loop state-table tests for vergil_tooling.lib.pr_merge."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.pr_merge import MergeAbort, wait_and_merge

_MOD = "vergil_tooling.lib.pr_merge"


def _gh(  # noqa: PLR0913 — test factory mirrors the engine's full input surface
    *,
    state: str = "OPEN",
    draft: bool = False,
    mergeable: str = "MERGEABLE",
    merge_states: list[str] | None = None,
    failed: list[str] | None = None,
) -> MagicMock:
    """Build a mocked github module for one scenario."""
    gh = MagicMock()
    gh.pr_state.return_value = state
    gh.is_draft.return_value = draft
    gh.mergeable.return_value = mergeable
    gh.merge_state_status.side_effect = merge_states or ["CLEAN", "CLEAN"]
    gh.failed_check_names.return_value = failed or []
    return gh


def test_green_first_try_merges() -> None:
    gh = _gh()
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="squash")
    gh.wait_for_checks.assert_called_once_with("99")
    gh.merge.assert_called_once_with("99", strategy="squash")
    gh.update_branch.assert_not_called()


def test_merged_on_entry_raises() -> None:
    gh = _gh(state="MERGED")
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbort, match="already merged"):
        wait_and_merge("99", strategy="squash")
    gh.merge.assert_not_called()


def test_draft_aborts_before_waiting() -> None:
    gh = _gh(draft=True)
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbort, match="draft"):
        wait_and_merge("99", strategy="squash")
    gh.wait_for_checks.assert_not_called()


def test_conflicting_aborts_before_waiting() -> None:
    gh = _gh(mergeable="CONFLICTING")
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbort, match="merge conflicts"):
        wait_and_merge("99", strategy="squash")
    gh.wait_for_checks.assert_not_called()


def test_behind_on_entry_updates_before_waiting() -> None:
    gh = _gh(merge_states=["BEHIND", "CLEAN", "CLEAN"])
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    gh.update_branch.assert_called_once_with("99")
    # update happened BEFORE the (single) wait — BEHIND-first ordering
    gh.wait_for_checks.assert_called_once_with("99")
    gh.merge.assert_called_once()


def test_behind_after_wait_loops_and_updates() -> None:
    # iteration 1: CLEAN pre-wait, BEHIND post-wait → loop
    # iteration 2: BEHIND pre-wait → update; iteration 3: CLEAN, CLEAN → merge
    gh = _gh(merge_states=["CLEAN", "BEHIND", "BEHIND", "CLEAN", "CLEAN"])
    with patch(_MOD + ".github", gh), patch(_MOD + ".time.sleep"):
        wait_and_merge("99", strategy="squash")
    gh.update_branch.assert_called_once_with("99")
    assert gh.wait_for_checks.call_count == 2
    gh.merge.assert_called_once()


def test_check_failure_aborts_with_names() -> None:
    gh = _gh(failed=["ci / test", "vergil-audit/approved"])
    with patch(_MOD + ".github", gh), pytest.raises(MergeAbort, match="ci / test"):
        wait_and_merge("99", strategy="squash")
    gh.merge.assert_not_called()


def test_merge_train_guard_exhausts() -> None:
    gh = _gh(merge_states=["BEHIND"] * 10)
    with (
        patch(_MOD + ".github", gh),
        patch(_MOD + ".time.sleep"),
        pytest.raises(MergeAbort, match="still behind"),
    ):
        wait_and_merge("99", strategy="squash")
    assert gh.update_branch.call_count == 5


def test_injected_wait_callable_is_used() -> None:
    gh = _gh()
    waiter = MagicMock()
    with patch(_MOD + ".github", gh):
        wait_and_merge("99", strategy="merge", wait_checks=waiter)
    waiter.assert_called_once_with("99")
    gh.wait_for_checks.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_pr_merge.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.pr_merge'`

- [ ] **Step 3: Write the implementation**

Create `src/vergil_tooling/lib/pr_merge.py`:

```python
"""Shared wait-and-merge engine with fail-fast ordering.

Used by ``vrg-finalize-pr`` (squash by default) and the release
workflow (merge strategy). Doomed outcomes — already merged, draft,
conflicting, behind — are checked *before* waiting, never after
letting a pointless CI run finish:

- MERGED: the caller's premise is wrong. What "already merged" means
  is a caller-level decision (finalize pre-checks and skips to
  cleanup; ``vrg-pr-await`` aborts per #1420), so the engine raises.
- Draft: can go green but ``gh pr merge`` refuses it.
- CONFLICTING: cannot merge no matter what CI says. Re-checked every
  iteration — a conflict can arise mid-loop when another PR merges.
- BEHIND: the current CI run is irrelevant; update-branch cancels it
  and starts a fresh one, so update immediately instead of waiting.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from vergil_tooling.lib import github

if TYPE_CHECKING:
    from collections.abc import Callable

_MAX_BRANCH_UPDATES = 5
_UPDATE_SETTLE_SECS = 5


class MergeAbort(Exception):
    """The PR cannot be merged; the message explains why and what to do."""


def wait_and_merge(
    pr: str,
    *,
    strategy: str,
    wait_checks: Callable[[str], None] | None = None,
) -> None:
    """Block until *pr* is green and current, then merge it.

    ``wait_checks`` lets callers substitute their own check-waiting
    primitive (the release workflow passes its verbose-aware wrapper);
    the default is ``github.wait_for_checks``.

    Raises ``MergeAbort`` on any unmergeable condition.
    """
    wait = wait_checks if wait_checks is not None else github.wait_for_checks
    updates = 0
    while True:
        if github.pr_state(pr) == "MERGED":
            msg = (
                f"PR {pr} is already merged — nothing to wait for. "
                "If cleanup is what remains, run vrg-finalize-pr without arguments."
            )
            raise MergeAbort(msg)
        if github.is_draft(pr):
            msg = f"PR {pr} is a draft — mark it ready (gh pr ready {pr}) and re-run."
            raise MergeAbort(msg)
        if github.mergeable(pr) == "CONFLICTING":
            msg = (
                f"PR {pr} has merge conflicts. Resolve them in the PR's worktree "
                "(merge the target branch in, push), then re-run."
            )
            raise MergeAbort(msg)
        if github.merge_state_status(pr) == "BEHIND":
            updates += 1
            if updates > _MAX_BRANCH_UPDATES:
                msg = (
                    f"PR {pr} still behind after {_MAX_BRANCH_UPDATES} branch updates "
                    "— the merge train is busy; re-run when it settles."
                )
                raise MergeAbort(msg)
            print("Branch is behind base — updating and re-checking...")
            github.update_branch(pr)
            time.sleep(_UPDATE_SETTLE_SECS)
            continue

        print(f"Waiting for checks on {pr}...")
        wait(pr)

        failed = github.failed_check_names(pr)
        if failed:
            msg = f"Checks failed on PR {pr}: {', '.join(failed)}"
            raise MergeAbort(msg)

        if github.merge_state_status(pr) == "BEHIND":
            continue  # something merged while we waited → update at loop top
        break

    print(f"Checks passed. Merging {pr} (--{strategy})...")
    github.merge(pr, strategy=strategy)
    print("Merged.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_pr_merge.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_merge.py tests/vergil_tooling/test_pr_merge.py
vrg-commit --type feat --scope pr-merge --message "add shared fail-fast wait-and-merge engine (#1423)"
```

---

### Task 5: release `wait_and_merge` becomes a thin wrapper

**Files:**
- Modify: `src/vergil_tooling/lib/release/merge.py` (full rewrite, it is 42 lines)
- Modify: `tests/vergil_tooling/test_release_merge.py`

- [ ] **Step 1: Rewrite the release merge tests**

Replace the loop-behavior tests in `tests/vergil_tooling/test_release_merge.py` with
wrapper-contract tests (read the existing file first; keep any test that already asserts
the wrapper contract):

```python
"""Tests for vergil_tooling.lib.release.merge (thin wrapper over pr_merge)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_merge import MergeAbort
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.merge import wait_and_merge

_MOD = "vergil_tooling.lib.release.merge"


def test_delegates_with_merge_strategy() -> None:
    with patch(_MOD + ".pr_merge.wait_and_merge") as engine:
        wait_and_merge("https://github.com/o/r/pull/5", phase="phase-2", verbose=True)
    engine.assert_called_once()
    args, kwargs = engine.call_args
    assert args == ("https://github.com/o/r/pull/5",)
    assert kwargs["strategy"] == "merge"
    assert callable(kwargs["wait_checks"])


def test_wraps_merge_abort_in_release_error() -> None:
    with (
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=MergeAbort("conflicts")),
        pytest.raises(ReleaseError) as excinfo,
    ):
        wait_and_merge("https://github.com/o/r/pull/5", phase="phase-3")
    assert excinfo.value.phase == "phase-3"
    assert "conflicts" in str(excinfo.value)
```

(Adapt the `ReleaseError` attribute assertions to its actual constructor — read
`src/vergil_tooling/lib/release/context.py` first; if `phase` is not a public attribute,
assert on the stringified error instead.)

- [ ] **Step 2: Run tests to verify the new ones fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_release_merge.py -v`
Expected: FAIL — the current implementation calls github functions directly, so
`_MOD + ".pr_merge..."` is not a valid patch target yet.

- [ ] **Step 3: Rewrite the implementation**

Replace the body of `src/vergil_tooling/lib/release/merge.py` with:

```python
"""Wait-poll-merge logic shared by Phases 2 and 3.

Thin wrapper over the shared engine in ``vergil_tooling.lib.pr_merge``
— release keeps its public interface (``ReleaseError`` on failure,
verbose-aware check waiting, merge-commit strategy) while the loop
logic lives in one place.
"""

from __future__ import annotations

from vergil_tooling.lib import pr_merge
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.subprocess import wait_for_checks


def wait_and_merge(pr_url: str, *, phase: str, verbose: bool = False) -> None:
    """Wait for checks, handle behind-base, then merge with a merge commit."""
    try:
        pr_merge.wait_and_merge(
            pr_url,
            strategy="merge",
            wait_checks=lambda pr: wait_for_checks(pr, verbose=verbose),
        )
    except pr_merge.MergeAbort as exc:
        raise ReleaseError(
            phase=phase,
            command="pr_merge.wait_and_merge",
            message=str(exc),
        ) from exc
```

(Keep the `ReleaseError(...)` keyword signature exactly as the current file uses it —
copy from the version being replaced.)

- [ ] **Step 4: Run release tests to verify pass + no regressions**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_release_merge.py tests/vergil_tooling/test_release_orchestrator.py tests/vergil_tooling/test_release.py -v`
Expected: PASS. If orchestrator tests patched the old internals
(`release.merge.github...`), repoint them to `release.merge.pr_merge.wait_and_merge`.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/merge.py tests/vergil_tooling/test_release_merge.py
vrg-commit --type refactor --scope release --message "delegate wait_and_merge to shared pr_merge engine (#1423)"
```

---

### Task 6: submit-pr — location resolution from the repo root

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_submit_pr.py`
- Modify: `tests/vergil_tooling/test_vrg_submit_pr.py`

- [ ] **Step 1: Add the autouse in-worktree fixture to existing tests**

The new preamble calls `git.is_main_worktree()`, which existing template-mode tests do
not patch. Add to `tests/vergil_tooling/test_vrg_submit_pr.py` (after the imports;
follow the autouse-fixture style of `test_vrg_finalize_pr.py`):

```python
_MOD = "vergil_tooling.bin.vrg_submit_pr"  # if not already defined


@pytest.fixture(autouse=True)
def _in_worktree() -> Iterator[None]:
    """Default every test to running inside a worktree (legacy behavior).

    Root-launch tests override by patching is_main_worktree directly —
    the innermost patch wins.
    """
    with patch(_MOD + ".git.is_main_worktree", return_value=False):
        yield
```

(Add `from collections.abc import Iterator` under `TYPE_CHECKING` if missing.)

- [ ] **Step 2: Write the failing root-launch tests**

Append to `tests/vergil_tooling/test_vrg_submit_pr.py`:

```python
def _wt(name: str, branch: str) -> "worktrees.Worktree":
    return worktrees.Worktree(path=Path(f"/repo/.worktrees/{name}"), branch=branch)


def test_root_single_ready_worktree_chdirs_and_submits(tmp_path: Path) -> None:
    wt = _wt("issue-7-foo", "feature/7-foo")
    fields = {"issue": "7", "title": "Foo title", "summary": "S"}
    with (
        patch(_MOD + ".identity_mode.is_agent", return_value=False),
        patch(_MOD + ".git.is_main_worktree", return_value=True),
        patch(_MOD + ".git.repo_root", return_value="/repo"),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".pr_template.read_template", return_value=fields),
        patch(_MOD + ".os.chdir") as chdir,
        patch(_MOD + ".git.current_branch", return_value="feature/7-foo"),
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    chdir.assert_called_once_with(wt.path)


def test_root_no_ready_worktrees_errors_with_reasons() -> None:
    wt = _wt("issue-7-foo", "feature/7-foo")
    with (
        patch(_MOD + ".identity_mode.is_agent", return_value=False),
        patch(_MOD + ".git.is_main_worktree", return_value=True),
        patch(_MOD + ".git.repo_root", return_value="/repo"),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[wt]),
        patch(_MOD + ".pr_template.read_template", side_effect=FileNotFoundError("x")),
        pytest.raises(SystemExit, match="no submittable worktrees"),
    ):
        main(["--dry-run"])


def test_root_multiple_ready_worktrees_prompts(tmp_path: Path) -> None:
    wts = [_wt("issue-7-foo", "feature/7-foo"), _wt("issue-8-bar", "feature/8-bar")]
    fields = {"issue": "8", "title": "Bar title", "summary": "S"}
    with (
        patch(_MOD + ".identity_mode.is_agent", return_value=False),
        patch(_MOD + ".git.is_main_worktree", return_value=True),
        patch(_MOD + ".git.repo_root", return_value="/repo"),
        patch(_MOD + ".worktrees.list_worktrees", return_value=wts),
        patch(_MOD + ".pr_template.read_template", return_value=fields),
        patch(_MOD + ".worktrees.require_tty"),
        patch(_MOD + ".prompt_choice", return_value="issue-8-bar — issue 8: Bar title"),
        patch(_MOD + ".os.chdir") as chdir,
        patch(_MOD + ".git.current_branch", return_value="feature/8-bar"),
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    chdir.assert_called_once_with(wts[1].path)
```

Add the needed imports at the top of the test file:
`from pathlib import Path`, `from vergil_tooling.lib import worktrees`, and extend the
`from vergil_tooling.bin.vrg_submit_pr import ...` line if `main` is not yet imported.

- [ ] **Step 3: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py -v -k root`
Expected: FAIL — `AttributeError` (no `worktrees`/`os` attributes on the module yet).

- [ ] **Step 4: Write the implementation**

In `src/vergil_tooling/bin/vrg_submit_pr.py`:

1. Extend imports:

```python
import os

from vergil_tooling.lib import git, github, identity_mode, pr_template, worktrees
from vergil_tooling.lib.repo_init import prompt_choice
```

2. Add the resolver before `_run_template_mode`:

```python
def _choose_submit_worktree(root: Path) -> Path:
    """At the repo root, pick the template-ready worktree to submit from.

    Candidates are worktrees containing a valid ``.vergil/pr-template.yml``
    — the agent-written signal that the issue is ready for submission.
    One candidate is auto-picked (the existing y/N preview still
    confirms); several prompt a menu; none is an error that names each
    skipped worktree and why.
    """
    ready: list[tuple[worktrees.Worktree, dict[str, str]]] = []
    skipped: list[str] = []
    for wt in worktrees.list_worktrees(root):
        try:
            fields = pr_template.read_template(wt.path)
        except FileNotFoundError:
            skipped.append(f"{wt.path.name}: no .vergil/pr-template.yml — not ready")
            continue
        except pr_template.TemplateError as exc:
            skipped.append(f"{wt.path.name}: {exc}")
            continue
        ready.append((wt, fields))

    if not ready:
        lines = ["vrg-submit-pr: no submittable worktrees found."]
        if skipped:
            lines.extend(f"  {reason}" for reason in skipped)
        else:
            lines.append("  (no .worktrees/ entries exist)")
        raise SystemExit("\n".join(lines))

    if len(ready) == 1:
        wt, fields = ready[0]
        print(f"Using worktree {wt.path.name} (issue {fields['issue']}: {fields['title']})")
        return wt.path

    labels = [f"{wt.path.name} — issue {f['issue']}: {f['title']}" for wt, f in ready]
    chosen = worktrees.select_worktree(
        [wt for wt, _ in ready],
        purpose="Multiple submittable worktrees",
        labels=labels,
    )
    return chosen.path
```

3. At the top of `_run_template_mode`, replace:

```python
def _run_template_mode(args: argparse.Namespace) -> int:
    root = Path(git.repo_root())
```

with:

```python
def _run_template_mode(args: argparse.Namespace) -> int:
    root = Path(git.repo_root())

    # Location resolution: from the main worktree (repo root), resolve
    # which `.worktrees/` worktree to submit from and move there. The
    # invoking shell is unaffected — chdir applies to this process only.
    if git.is_main_worktree():
        target = _choose_submit_worktree(root)
        os.chdir(target)
        root = target
```

(Everything after — `pr_template.read_template(root)`, branch detection, push, PR
creation, `pr_template.delete_template(root)` — now operates on the worktree. No other
changes in this function.)

- [ ] **Step 5: Run the full submit-pr test file**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py -v`
Expected: PASS (new root tests + all pre-existing tests via the autouse fixture)

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_submit_pr.py tests/vergil_tooling/test_vrg_submit_pr.py
vrg-commit --type feat --scope submit-pr --message "resolve target worktree when run from the repo root (#1423)"
```

---

### Task 7: finalize-pr — PR inference and always-confirm

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py`
- Modify: `tests/vergil_tooling/test_vrg_finalize_pr.py`

- [ ] **Step 1: Add the autouse interactive fixture to existing tests**

Existing no-arg tests exercise the cleanup path, which now gains a confirmation prompt
and TTY guard. Add to `tests/vergil_tooling/test_vrg_finalize_pr.py` next to the other
autouse fixtures:

```python
@pytest.fixture(autouse=True)
def _interactive_defaults() -> Iterator[None]:
    """Default: TTY present, no worktree candidates, cleanup confirmed.

    Inference tests override by patching these targets directly — the
    innermost patch wins.
    """
    with (
        patch(_MOD + ".worktrees.require_tty"),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[]),
        patch(_MOD + ".prompt_yes_no", return_value=True),
    ):
        yield
```

- [ ] **Step 2: Write the failing inference tests**

Append to `tests/vergil_tooling/test_vrg_finalize_pr.py` (the dry-run flag keeps these
tests off the real cleanup path; `_finalize_specific_pr` internals are patched where
needed):

```python
from vergil_tooling.lib.worktrees import Worktree

_PR7 = {"number": "7", "url": "https://github.com/o/r/pull/7", "title": "Foo"}
_PR8 = {"number": "8", "url": "https://github.com/o/r/pull/8", "title": "Bar"}
_WT7 = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
_WT8 = Worktree(path=Path("/repo/.worktrees/issue-8-bar"), branch="feature/8-bar")


@contextmanager
def _cleanup_path_mocks() -> Iterator[None]:
    """Neutralize the post-merge cleanup path for inference-focused tests.

    Keeps main() off real git/config/gh calls after the part under test.
    (github.head_ref exists from Task 3; the patch is inert until Task 8
    wires it into main().)
    """
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + ".git.read_output", return_value=""),
    ):
        yield


def test_no_arg_single_candidate_confirms_and_finalizes() -> None:
    with (
        _cleanup_path_mocks(),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=_PR7),
        patch(_MOD + ".prompt_yes_no", return_value=True) as confirm,
        patch(_MOD + "._finalize_specific_pr", return_value=0) as fin,
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    assert "PR #7" in confirm.call_args[0][0]
    assert fin.call_args[0][0].pr == "https://github.com/o/r/pull/7"


def test_no_arg_single_candidate_decline_exits_zero_without_action() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=_PR7),
        patch(_MOD + ".prompt_yes_no", return_value=False),
        patch(_MOD + "._finalize_specific_pr") as fin,
        patch(_MOD + ".git.current_branch") as branch,
    ):
        rc = main([])
    assert rc == 0
    fin.assert_not_called()
    branch.assert_not_called()  # cleanup never started


def test_no_arg_multiple_candidates_menu_then_confirm() -> None:
    def _pr_for(branch: str) -> dict[str, str]:
        return _PR7 if branch == "feature/7-foo" else _PR8

    with (
        _cleanup_path_mocks(),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7, _WT8]),
        patch(_MOD + ".github.pr_for_branch", side_effect=_pr_for),
        patch(_MOD + ".prompt_choice", return_value="PR #8 (feature/8-bar): Bar") as menu,
        patch(_MOD + ".prompt_yes_no", return_value=True),
        patch(_MOD + "._finalize_specific_pr", return_value=0) as fin,
    ):
        rc = main(["--dry-run"])
    assert rc == 0
    menu.assert_called_once()
    assert fin.call_args[0][0].pr == "https://github.com/o/r/pull/8"


def test_no_arg_no_candidates_confirms_cleanup_only() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees", return_value=[_WT7]),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".prompt_yes_no", return_value=False) as confirm,
    ):
        rc = main([])
    assert rc == 0
    assert "cleanup only" in confirm.call_args[0][0].lower()


def test_explicit_pr_skips_inference_and_prompts() -> None:
    with (
        patch(_MOD + ".worktrees.list_worktrees") as listing,
        patch(_MOD + ".prompt_yes_no") as confirm,
        patch(_MOD + "._finalize_specific_pr", return_value=0) as fin,
    ):
        rc = main(["123", "--dry-run"])
    assert rc == 0
    listing.assert_not_called()
    confirm.assert_not_called()
    fin.assert_called_once()
```

(`Path` and `main` are already imported in this file; add
`from contextlib import contextmanager` and verify/extend the other imports as needed.)

- [ ] **Step 3: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v -k "candidate or cleanup_only or skips_inference"`
Expected: FAIL — no-arg runs go straight to cleanup; explicit-arg assertions on
inference don't hold yet.

- [ ] **Step 4: Write the implementation**

In `src/vergil_tooling/bin/vrg_finalize_pr.py`:

1. Extend imports:

```python
from vergil_tooling.lib import config, git, github, pr_provenance, worktrees
from vergil_tooling.lib.repo_init import prompt_choice, prompt_yes_no
```

2. Add the inference helper after `_worktree_is_dirty`:

```python
def _infer_pr(root: Path, target_branch: str) -> str | None:
    """Resolve which PR to finalize when none was given; always confirm.

    Returns the PR URL to finalize, or None when the user chose (or
    confirmed) cleanup-only / declined entirely. Raises SystemExit via
    require_tty when stdin is not interactive — these prompts are the
    human touch point of the workflow, and the explicit-PR argument is
    the scriptable path.
    """
    worktrees.require_tty("vrg-finalize-pr without a PR argument")

    pairs: list[tuple[worktrees.Worktree, dict[str, str]]] = []
    for wt in worktrees.list_worktrees(root):
        pr = github.pr_for_branch(wt.branch)
        if pr is not None:
            pairs.append((wt, pr))

    if not pairs:
        confirmed = prompt_yes_no(
            f"No open PRs found in worktrees. Run cleanup only (switch to "
            f"{target_branch}, pull, prune branches/worktrees)?",
            default=False,
        )
        if not confirmed:
            print("Aborted.")
            raise SystemExit(0)
        return None

    if len(pairs) == 1:
        wt, pr = pairs[0]
    else:
        labels = [f"PR #{p['number']} ({w.branch}): {p['title']}" for w, p in pairs]
        chosen = prompt_choice("Multiple PRs ready to finalize", labels)
        wt, pr = pairs[labels.index(chosen)]

    if not prompt_yes_no(f"Finalize PR #{pr['number']} ({pr['title']})?", default=False):
        print("Aborted.")
        raise SystemExit(0)
    return pr["url"]
```

3. In `main()`, after the `root = git.repo_root()` line, replace:

```python
    if args.pr is not None:
        rc = _finalize_specific_pr(args)
        if rc != 0:
            return rc
```

with:

```python
    if args.pr is None:
        try:
            args.pr = _infer_pr(root, args.target_branch)
        except SystemExit as exc:
            if exc.code == 0:
                return 0
            raise

    if args.pr is not None:
        rc = _finalize_specific_pr(args)
        if rc != 0:
            return rc
```

- [ ] **Step 5: Run the full finalize test file**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v`
Expected: PASS (new inference tests + pre-existing tests via the autouse fixture)

- [ ] **Step 6: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type feat --scope finalize --message "infer the target PR from worktrees and always confirm (#1423)"
```

---

### Task 8: finalize-pr — engine swap and explicit-target cleanup

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py`
- Modify: `tests/vergil_tooling/test_vrg_finalize_pr.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/vergil_tooling/test_vrg_finalize_pr.py`:

```python
from vergil_tooling.lib.pr_merge import MergeAbort

_CLEAN_PROVENANCE = ProvenanceResult(violations=[], advisories=[])


def test_finalize_specific_pr_uses_wait_and_merge() -> None:
    args = parse_args(["123"])
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
    ):
        rc = _finalize_specific_pr(args)
    assert rc == 0
    engine.assert_called_once_with("123", strategy="squash")


def test_finalize_specific_pr_merge_abort_returns_error() -> None:
    args = parse_args(["123"])
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="OPEN"),
        patch(_MOD + ".pr_merge.wait_and_merge", side_effect=MergeAbort("draft")),
    ):
        rc = _finalize_specific_pr(args)
    assert rc == 1


def test_finalize_specific_pr_already_merged_skips_engine() -> None:
    args = parse_args(["123"])
    with (
        patch(_MOD + ".pr_provenance.check_pr", return_value=_CLEAN_PROVENANCE),
        patch(_MOD + ".github.pr_state", return_value="MERGED"),
        patch(_MOD + ".pr_merge.wait_and_merge") as engine,
    ):
        rc = _finalize_specific_pr(args)
    assert rc == 0
    engine.assert_not_called()


def test_explicit_target_cleanup_deletes_merged_pr_branch() -> None:
    """After a squash merge, the just-merged branch is cleaned even though
    `git branch --merged` cannot see it."""
    with (
        patch(_MOD + ".prompt_yes_no") as confirm,
        patch(_MOD + "._finalize_specific_pr", return_value=0),
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".github.head_ref", return_value="feature/7-foo"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run"),
        patch(_MOD + ".git.merged_branches", return_value=[]),  # squash: sweep is blind
        patch(_MOD + ".git.read_output", return_value="feature/7-foo"),
        patch(_MOD + ".worktrees.worktree_for_branch", return_value=None),
        patch(_MOD + ".clean_branch_images", return_value=0),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as proc,
    ):
        proc.return_value.returncode = 0
        rc = main(["123"])
    assert rc == 0
    confirm.assert_not_called()  # explicit arg: no prompt


def test_explicit_target_cleanup_respects_eternal_branches() -> None:
    with (
        patch(_MOD + "._finalize_specific_pr", return_value=0),
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".github.head_ref", return_value="develop"),
        patch(_MOD + ".config.read_config", side_effect=FileNotFoundError),
        patch(_MOD + ".git.current_branch", return_value="develop"),
        patch(_MOD + ".git.run") as git_run,
        patch(_MOD + ".git.merged_branches", return_value=[]),
        patch(_MOD + "._check_cd_workflow_status", return_value=None),
        patch(_MOD + ".subprocess.run") as proc,
        patch(_MOD + ".Path.is_file", return_value=False),
    ):
        proc.return_value.returncode = 0
        rc = main(["123"])
    assert rc == 0
    for call in git_run.call_args_list:
        assert call.args[:2] != ("branch", "-D"), "must never delete an eternal branch"
```

(Extend the import of `_finalize_specific_pr` from the module if not already imported;
`ProvenanceResult` is already imported in this file — match its actual constructor,
reading `lib/pr_provenance.py` if the keyword form differs.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v -k "wait_and_merge or merge_abort or skips_engine or explicit_target"`
Expected: FAIL — `pr_merge`/`head_ref` are not used by the module yet.

- [ ] **Step 3: Write the implementation**

In `src/vergil_tooling/bin/vrg_finalize_pr.py`:

1. Extend imports:

```python
from vergil_tooling.lib import config, git, github, pr_merge, pr_provenance, worktrees
```

2. In `_finalize_specific_pr`, replace the merge block:

```python
    if github.pr_state(args.pr) == "MERGED":
        print(f"PR {args.pr} already merged.")
    elif args.dry_run:
        print(f"  [dry-run] merge PR {args.pr} (--{args.strategy})")
    else:
        print(f"Merging PR {args.pr} (--{args.strategy})...")
        github.merge(args.pr, strategy=args.strategy)

    return 0
```

with:

```python
    if github.pr_state(args.pr) == "MERGED":
        print(f"PR {args.pr} already merged.")
    elif args.dry_run:
        print(f"  [dry-run] wait for green, then merge PR {args.pr} (--{args.strategy})")
    else:
        try:
            pr_merge.wait_and_merge(args.pr, strategy=args.strategy)
        except pr_merge.MergeAbort as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    return 0
```

3. Extract the per-branch deletion body into a helper (place above `main()`). This is
   the existing sweep-loop body, factored so the explicit-target step and the sweep
   share one implementation:

```python
def _delete_branch_and_worktree(branch: str, root: Path, *, dry_run: bool) -> bool:
    """Remove *branch* and its canonical worktree; True if the branch was deleted.

    Shared by the explicit-target step (the PR branch just merged, which
    a squash merge hides from ``git branch --merged``) and the ancestry
    sweep for stragglers.
    """
    wt = worktrees.worktree_for_branch(branch, root)
    if wt is not None:
        if _worktree_is_dirty(wt):
            print(f"  Skipping {branch}: worktree {wt} has uncommitted changes")
            return False
        print(f"  Removing worktree: {wt}")
        _run(["worktree", "remove", str(wt)], dry_run=dry_run)
    print(f"  Deleting merged branch: {branch}")
    _run(["branch", "-D", branch], dry_run=dry_run)
    if not dry_run:
        removed = clean_branch_images(branch)
        if removed:
            print(f"  Cleaned {removed} cached container image(s) for {branch}")
    return True
```

4. In `main()`, capture the merged PR's head branch. Directly after the
   `if args.pr is not None:` block that calls `_finalize_specific_pr`, add:

```python
    merged_branch: str | None = None
    if args.pr is not None:
        merged_branch = github.head_ref(args.pr)
```

5. Replace the sweep loop body with calls to the helper, preceded by the
   explicit-target step. The current code:

```python
    print("Checking for merged local branches...")
    deleted: list[str] = []
    for branch in git.merged_branches(args.target_branch):
        if branch in eternal:
            continue
        # ... worktree removal + branch -D + clean_branch_images ...
```

becomes:

```python
    deleted: list[str] = []

    # Explicit-target cleanup: the just-merged PR branch. The default
    # squash strategy rewrites history onto the target, so the branch is
    # never an ancestor and `git branch --merged` cannot see it — without
    # this step the flagship flow would merge and then silently fail to
    # clean up the very worktree it inferred the PR from.
    if merged_branch and merged_branch not in eternal:
        if git.read_output("branch", "--list", merged_branch):
            print(f"Cleaning up merged PR branch {merged_branch}...")
            if _delete_branch_and_worktree(merged_branch, root, dry_run=args.dry_run):
                deleted.append(merged_branch)
        else:
            print(f"  Merged PR branch {merged_branch} has no local branch — skipping.")

    print("Checking for merged local branches...")
    for branch in git.merged_branches(args.target_branch):
        if branch in eternal or branch in deleted:
            continue
        if _delete_branch_and_worktree(branch, root, dry_run=args.dry_run):
            deleted.append(branch)
```

(Keep the explanatory comments from the old loop body with the helper — they document
the `-D` and Issue #315/#307 reasoning.)

- [ ] **Step 4: Run the full finalize test file**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v`
Expected: PASS — including all pre-existing merge/cleanup tests. Pre-existing tests
that patched `_MOD + ".github.merge"` for the PR path must be repointed to
`_MOD + ".pr_merge.wait_and_merge"`; tests exercising `main()` with a PR argument also
need `patch(_MOD + ".github.head_ref", return_value="feature/x")` added.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type feat --scope finalize --message "wait for green before merging and clean the squash-merged branch (#1423)"
```

---

### Task 9: reference documentation

**Files:**
- Modify: `docs/site/docs/reference/dev/submit-pr.md`
- Modify: `docs/site/docs/reference/dev/finalize-pr.md`

- [ ] **Step 1: Read both pages, then update them**

Read each page first and integrate the following content where each page describes
invocation/behavior (adapt headings to the page's existing structure — do not duplicate
sections that already exist; rewrite them).

For `submit-pr.md`, the root-launch behavior:

```markdown
## Running from the repo root

`vrg-submit-pr` (no arguments) may be run from the repo root — it
resolves the target worktree itself:

- **One submittable worktree** (contains `.vergil/pr-template.yml`):
  announced and entered automatically; the usual preview + `[y/N]`
  confirmation follows.
- **Several submittable worktrees:** a numbered menu shows each
  worktree with its issue number and title; pick one.
- **None:** an error lists each worktree and why it was skipped.

Run from inside a worktree, behavior is unchanged. The tool is
interactive by design — it is a human touch point of the workflow and
requires a terminal.
```

For `finalize-pr.md`, the inference + wait behavior:

```markdown
## Choosing the PR

- `vrg-finalize-pr <pr-url-or-number>` — no prompts; the explicit
  argument is the confirmation. This is the scriptable path.
- `vrg-finalize-pr` (no arguments) — infers candidates by mapping each
  `.worktrees/` worktree's branch to its open PR, and **always
  confirms before acting**: one candidate asks `Finalize PR #N?`;
  several present a menu; none asks before running cleanup-only.

## Waiting for green

When the PR's checks are not finished, `vrg-finalize-pr` waits for
them and merges automatically once everything is green and current.
Doomed outcomes abort immediately rather than after the wait: a draft
PR, merge conflicts, a failed check (named in the error), or a branch
still behind after five update attempts. A branch that is merely
behind the target is updated automatically and the wait restarts.

After the merge, the PR's own branch and worktree are cleaned up
explicitly (a squash merge hides them from `git branch --merged`),
followed by the usual sweep, pull, and prune.
```

- [ ] **Step 2: Verify docs build/lint via validation**

Run: `vrg-container-run -- uv run vrg-validate`
Expected: PASS (markdownlint covers these files; fix any line-length/style complaints)

- [ ] **Step 3: Commit**

```bash
vrg-git add docs/site/docs/reference/dev/submit-pr.md docs/site/docs/reference/dev/finalize-pr.md
vrg-commit --type docs --scope reference --message "document root launch, PR inference, and wait-for-green (#1423)"
```

---

### Task 10: full validation and handoff

- [ ] **Step 1: Run the complete validation pipeline**

Run: `vrg-container-run -- uv run vrg-validate`
Expected: PASS — lint, typecheck, full test suite, audit, common checks. Fix anything
red before proceeding (correctness > cost > speed; never hand off known-broken work).

- [ ] **Step 2: Update the spec status line**

In `docs/specs/2026-06-05-pr-interface-design.md`, change:

```markdown
- **Status:** Approved design, pending implementation plan
```

to:

```markdown
- **Status:** Approved — implemented (see docs/plans/2026-06-05-pr-interface.md)
```

- [ ] **Step 3: Write the PR template for human handoff**

Write `.vergil/pr-template.yml` in the worktree (agents must not run `vrg-submit-pr` —
the human submits):

```yaml
issue: 1423
title: "feat(pr-interface): worktree-aware submit and wait-for-green finalize"
summary: |
  vrg-submit-pr resolves its worktree from the repo root (menu when several are
  template-ready); vrg-finalize-pr infers the PR from worktrees, always confirms,
  waits for green with fail-fast ordering (draft/conflict/behind checked before
  waiting), and explicitly cleans the squash-merged branch and worktree. Shared
  engines live in lib/worktrees.py and lib/pr_merge.py; the release workflow
  delegates to the same merge engine.
```

- [ ] **Step 4: Commit and stop**

```bash
vrg-git add docs/specs/2026-06-05-pr-interface-design.md
vrg-commit --type docs --scope specs --message "mark PR interface design as implemented (#1423)"
```

Stop here. PR submission, merge, and finalization are **human actions** — report
completion and hand off.
