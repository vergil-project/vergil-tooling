# Batch PR pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `vrg-submit-pr` and `vrg-finalize-pr` drive a *batch* of PRs through the existing submit → finalize → release pipeline single-threaded, so each expensive CI gate runs exactly once.

**Architecture:** A new generic orchestrator (`lib/pr_workflow/batch.py`) runs items through a per-item callback serially, fail-fast, then runs end-of-batch post-steps (validation, one release) only on full success. `vrg-submit-pr` (rebase → submit → finalize-item) and `vrg-finalize-pr` (merge-item) each build the item list and per-item callback, reusing the existing single-PR machinery via subprocess chaining (the established pattern). One up-front confirmation; per-item prompts are pre-suppressed.

**Tech Stack:** Python 3.12+, argparse, `subprocess`, pytest with `unittest.mock`. Spec: `docs/specs/2026-06-17-batch-pr-pipeline-design.md`.

**Conventions for every commit in this plan:**
- Use `vrg-commit --type <t> --scope <s> --message "<m>" --body "<b>"` — raw `git commit` is denied. `vrg-commit` resolves co-authors itself; do not add a `Co-Authored-By` trailer.
- Stage with `vrg-git add <paths>` before committing.
- Run all work from inside the worktree `.worktrees/issue-1673-batch-pr/`.
- Validation command (whole suite): `vrg-container-run -- vrg-validate`. To run one test file fast during a task, use `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest <path> -v` (the dev-tree override venv; see CLAUDE.md). Final gate before any PR is always `vrg-container-run -- vrg-validate`.

---

## File structure

**New files:**
- `src/vergil_tooling/lib/pr_workflow/batch.py` — generic serial orchestrator + report types.
- `tests/vergil_tooling/pr_workflow/test_batch.py` — orchestrator unit tests.

**Modified files:**
- `src/vergil_tooling/lib/repo_init.py` — add `prompt_multi_choice`.
- `src/vergil_tooling/lib/worktrees.py` — add `select_worktrees`, `match_worktrees`, `rebase_onto`.
- `src/vergil_tooling/bin/vrg_finalize_pr.py` — `--skip-post-checks` flag; comma-list / `--all` batch mode.
- `src/vergil_tooling/bin/vrg_submit_pr.py` — `--all` / `--select` flags; factor `_submit_one`; batch mode.
- `tests/vergil_tooling/test_repo_init.py`, `test_worktrees.py`, `test_vrg_finalize_pr.py`, `test_vrg_submit_pr.py` — extend.

---

## Task 1: Batch report types

**Files:**
- Create: `src/vergil_tooling/lib/pr_workflow/batch.py`
- Test: `tests/vergil_tooling/pr_workflow/test_batch.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/pr_workflow/test_batch.py
"""Tests for the batch orchestrator (vergil_tooling.lib.pr_workflow.batch)."""

from __future__ import annotations

from vergil_tooling.lib.pr_workflow.batch import (
    BatchReport,
    ItemOutcome,
    ItemResult,
)


def test_all_merged_true_only_when_every_item_merged() -> None:
    merged = BatchReport(items=[ItemResult("a", ItemOutcome.MERGED)])
    assert merged.all_merged is True

    mixed = BatchReport(
        items=[
            ItemResult("a", ItemOutcome.MERGED),
            ItemResult("b", ItemOutcome.FAILED, "boom"),
        ]
    )
    assert mixed.all_merged is False


def test_all_merged_false_when_empty() -> None:
    assert BatchReport().all_merged is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/pr_workflow/test_batch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.pr_workflow.batch'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/vergil_tooling/lib/pr_workflow/batch.py
"""Single-threaded, fail-fast batch orchestrator for the PR pipeline.

Runs a sequence of items through a per-item ``process`` callback one at a
time, stopping at the first failure (fail-fast). Completed items are
reported MERGED, the failed one FAILED with its reason, and the rest
NOT_STARTED. Post-steps (end-of-batch validation, a single release) run
only when every item merged cleanly.

The whole run is gated by exactly one up-front confirmation: per-item
prompts are pre-suppressed by the callers (they thread ``assume_yes``), so
once confirmed the batch runs unattended. (Issue #1673.)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from vergil_tooling.lib.confirm import confirm

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class BatchAbort(Exception):
    """A per-item step (or post-step) failed; the message is the reason.

    Callers convert expected failures (rebase conflict, gate red, merge
    abort, provenance violation, non-zero subprocess) into this so the
    orchestrator can record them and stop without masking unexpected bugs,
    which propagate.
    """


class ItemOutcome(StrEnum):
    MERGED = "merged"
    FAILED = "failed"
    NOT_STARTED = "not-started"


@dataclass(frozen=True)
class ItemResult:
    label: str
    outcome: ItemOutcome
    reason: str | None = None


@dataclass(frozen=True)
class PostStep:
    name: str
    run: Callable[[], None]


@dataclass
class BatchReport:
    items: list[ItemResult] = field(default_factory=list)
    post_failure: str | None = None

    @property
    def all_merged(self) -> bool:
        return bool(self.items) and all(i.outcome is ItemOutcome.MERGED for i in self.items)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/pr_workflow/test_batch.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/batch.py tests/vergil_tooling/pr_workflow/test_batch.py
vrg-commit --type feat --scope batch --message "add batch report types (#1673)" --body "ItemOutcome/ItemResult/BatchReport/PostStep/BatchAbort scaffold for the serial PR batch orchestrator. Ref #1673"
```

---

## Task 2: `run_batch` orchestrator loop

**Files:**
- Modify: `src/vergil_tooling/lib/pr_workflow/batch.py` (append `run_batch` + `format_report`)
- Test: `tests/vergil_tooling/pr_workflow/test_batch.py` (append)

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/vergil_tooling/pr_workflow/test_batch.py
from unittest.mock import patch

from vergil_tooling.lib.pr_workflow.batch import (
    BatchAbort,
    PostStep,
    format_report,
    run_batch,
)

_MOD = "vergil_tooling.lib.pr_workflow.batch"


def _confirm_yes():
    return patch(_MOD + ".confirm", return_value=True)


def test_all_items_processed_in_order_on_success() -> None:
    seen: list[str] = []
    with _confirm_yes():
        report = run_batch(
            ["a", "b", "c"],
            process=seen.append,
            label=lambda it: it,
            plan=["do a", "do b", "do c"],
            assume_yes=True,
        )
    assert seen == ["a", "b", "c"]
    assert report.all_merged is True


def test_first_failure_stops_and_marks_rest_not_started() -> None:
    def process(it: str) -> None:
        if it == "b":
            raise BatchAbort("gate red")

    with _confirm_yes():
        report = run_batch(
            ["a", "b", "c"],
            process=process,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
        )
    outcomes = [(i.label, i.outcome.value, i.reason) for i in report.items]
    assert outcomes == [
        ("a", "merged", None),
        ("b", "failed", "gate red"),
        ("c", "not-started", None),
    ]
    assert report.all_merged is False


def test_post_steps_run_once_on_full_success() -> None:
    calls: list[str] = []
    with _confirm_yes():
        run_batch(
            ["a"],
            process=lambda it: None,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            post_steps=[PostStep("release", lambda: calls.append("release"))],
        )
    assert calls == ["release"]


def test_post_steps_skipped_when_any_item_failed() -> None:
    calls: list[str] = []

    def process(it: str) -> None:
        raise BatchAbort("nope")

    with _confirm_yes():
        report = run_batch(
            ["a"],
            process=process,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            post_steps=[PostStep("release", lambda: calls.append("release"))],
        )
    assert calls == []
    assert report.post_failure is None


def test_post_step_failure_recorded_not_raised() -> None:
    def boom() -> None:
        raise BatchAbort("release blew up")

    with _confirm_yes():
        report = run_batch(
            ["a"],
            process=lambda it: None,
            label=lambda it: it,
            plan=[],
            assume_yes=True,
            post_steps=[PostStep("release", boom)],
        )
    assert report.post_failure == "release: release blew up"


def test_decline_marks_all_not_started_and_runs_nothing() -> None:
    seen: list[str] = []
    with patch(_MOD + ".confirm", return_value=False):
        report = run_batch(
            ["a", "b"],
            process=seen.append,
            label=lambda it: it,
            plan=[],
            assume_yes=False,
        )
    assert seen == []
    assert [i.outcome.value for i in report.items] == ["not-started", "not-started"]


def test_format_report_groups_buckets() -> None:
    report = BatchReport(
        items=[
            ItemResult("a", ItemOutcome.MERGED),
            ItemResult("b", ItemOutcome.FAILED, "gate red"),
            ItemResult("c", ItemOutcome.NOT_STARTED),
        ]
    )
    out = format_report(report)
    assert "Merged:" in out
    assert "a" in out
    assert "Failed:" in out
    assert "b — gate red" in out
    assert "Not started:" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/pr_workflow/test_batch.py -v`
Expected: FAIL — `ImportError: cannot import name 'run_batch'`.

- [ ] **Step 3: Write implementation (append to `batch.py`)**

```python
def run_batch(
    items: Sequence[Any],
    process: Callable[[Any], None],
    *,
    label: Callable[[Any], str],
    plan: Sequence[str],
    assume_yes: bool,
    post_steps: Sequence[PostStep] = (),
) -> BatchReport:
    """Run *items* through *process* serially, fail-fast, then *post_steps*.

    Prints *plan* and asks exactly one confirmation (skipped with
    *assume_yes*). On decline, returns an all-NOT_STARTED report and runs
    nothing. Each item that raises ``BatchAbort`` stops the batch: it is
    recorded FAILED and the remaining items NOT_STARTED. ``post_steps`` run
    in order only when every item merged; a post-step ``BatchAbort`` is
    recorded in ``post_failure`` (never un-doing a merge) and stops the
    remaining post-steps.
    """
    report = BatchReport()

    print("Batch plan:")
    for line in plan:
        print(f"  {line}")
    if not confirm("\nRun this batch?", assume_yes=assume_yes, default=False):
        print("Aborted.")
        report.items = [ItemResult(label(it), ItemOutcome.NOT_STARTED) for it in items]
        return report

    stopped = False
    for it in items:
        if stopped:
            report.items.append(ItemResult(label(it), ItemOutcome.NOT_STARTED))
            continue
        try:
            process(it)
        except BatchAbort as exc:
            report.items.append(ItemResult(label(it), ItemOutcome.FAILED, str(exc)))
            stopped = True
        else:
            report.items.append(ItemResult(label(it), ItemOutcome.MERGED))

    if report.all_merged:
        for step in post_steps:
            try:
                step.run()
            except BatchAbort as exc:
                report.post_failure = f"{step.name}: {exc}"
                break

    return report


def format_report(report: BatchReport) -> str:
    """Render the merged / failed / not-started buckets as a summary block."""
    lines = ["", "Batch summary:"]
    for bucket, outcome in (
        ("Merged", ItemOutcome.MERGED),
        ("Failed", ItemOutcome.FAILED),
        ("Not started", ItemOutcome.NOT_STARTED),
    ):
        members = [i for i in report.items if i.outcome is outcome]
        if not members:
            continue
        lines.append(f"  {bucket}:")
        for i in members:
            suffix = f" — {i.reason}" if i.reason else ""
            lines.append(f"    {i.label}{suffix}")
    if report.post_failure:
        lines.append(f"  Post-step failed: {report.post_failure}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/pr_workflow/test_batch.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/pr_workflow/batch.py tests/vergil_tooling/pr_workflow/test_batch.py
vrg-commit --type feat --scope batch --message "add run_batch serial orchestrator (#1673)" --body "Fail-fast serial loop with one up-front confirm, NOT_STARTED for the tail after a failure, and post-steps that run only on full success. Ref #1673"
```

---

## Task 3: `prompt_multi_choice` (terminal multi-select)

**Files:**
- Modify: `src/vergil_tooling/lib/repo_init.py` (add `prompt_multi_choice` next to `prompt_choice`)
- Test: `tests/vergil_tooling/test_repo_init.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/vergil_tooling/test_repo_init.py  (create if absent; otherwise append)
"""Tests for vergil_tooling.lib.repo_init prompts."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib.repo_init import prompt_multi_choice

_MOD = "vergil_tooling.lib.repo_init"


def test_multi_choice_numbers() -> None:
    with patch("builtins.input", return_value="1,3"):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [0, 2]


def test_multi_choice_all() -> None:
    with patch("builtins.input", return_value="all"):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [0, 1, 2]


def test_multi_choice_space_separated_and_dedup_sorted() -> None:
    with patch("builtins.input", return_value="3 1 1"):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [0, 2]


def test_multi_choice_reprompts_on_out_of_range(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("builtins.input", side_effect=["9", "2"]):
        assert prompt_multi_choice("pick", ["a", "b", "c"]) == [1]
    assert "between 1 and 3" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_repo_init.py -v`
Expected: FAIL — `ImportError: cannot import name 'prompt_multi_choice'`.

- [ ] **Step 3: Write implementation (add to `repo_init.py`, after `prompt_choice`)**

```python
def prompt_multi_choice(label: str, options: list[str]) -> list[int]:
    """Present a numbered list; return the chosen 0-based indices.

    Accepts a comma- or space-separated list of numbers, or ``all`` for
    every option. Re-prompts on invalid or out-of-range input or an empty
    selection — the caller wants at least one.
    """
    print(f"\n{label}:")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")
    while True:
        raw = input("  Select (comma-separated numbers, or 'all'): ").strip().lower()
        if raw == "all":
            return list(range(len(options)))
        tokens = [t for t in raw.replace(",", " ").split() if t]
        try:
            chosen = sorted({int(t) for t in tokens})
        except ValueError:
            print(f"  Enter numbers between 1 and {len(options)}, or 'all'.")
            continue
        if chosen and all(1 <= n <= len(options) for n in chosen):
            return [n - 1 for n in chosen]
        print(f"  Enter numbers between 1 and {len(options)}, or 'all'.")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_repo_init.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/repo_init.py tests/vergil_tooling/test_repo_init.py
vrg-commit --type feat --scope repo-init --message "add prompt_multi_choice (#1673)" --body "Terminal checkbox-style multi-select: comma/space numbers or 'all'. Used by worktree batch selection. Ref #1673"
```

---

## Task 4: `worktrees.select_worktrees` and `match_worktrees`

**Files:**
- Modify: `src/vergil_tooling/lib/worktrees.py` (add two functions; import `prompt_multi_choice`)
- Test: `tests/vergil_tooling/test_worktrees.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/vergil_tooling/test_worktrees.py
from pathlib import Path
from unittest.mock import patch

import pytest

from vergil_tooling.lib.worktrees import Worktree, match_worktrees, select_worktrees

_WT_MOD = "vergil_tooling.lib.worktrees"


def _wt(name: str, branch: str) -> Worktree:
    return Worktree(path=Path(f"/repo/.worktrees/{name}"), branch=branch)


def test_select_worktrees_single_skips_prompt() -> None:
    wt = _wt("issue-1-foo", "feature/1-foo")
    assert select_worktrees([wt], purpose="p", labels=["foo"]) == [wt]


def test_select_worktrees_multi_uses_prompt_indices() -> None:
    a = _wt("issue-1-a", "feature/1-a")
    b = _wt("issue-2-b", "feature/2-b")
    c = _wt("issue-3-c", "feature/3-c")
    with (
        patch(_WT_MOD + ".require_tty"),
        patch(_WT_MOD + ".prompt_multi_choice", return_value=[0, 2]),
    ):
        assert select_worktrees([a, b, c], purpose="p", labels=["a", "b", "c"]) == [a, c]


def test_match_worktrees_by_issue_number_and_name_in_token_order() -> None:
    a = _wt("issue-1673-foo", "feature/1673-foo")
    b = _wt("issue-1681-bar", "feature/1681-bar")
    assert match_worktrees([a, b], ["1681", "issue-1673-foo"]) == [b, a]


def test_match_worktrees_unmatched_token_errors() -> None:
    a = _wt("issue-1-a", "feature/1-a")
    with pytest.raises(ValueError, match="no ready worktree matches: 999"):
        match_worktrees([a], ["999"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_worktrees.py -v -k "select_worktrees or match_worktrees"`
Expected: FAIL — `ImportError: cannot import name 'match_worktrees'`.

- [ ] **Step 3: Write implementation (add to `worktrees.py`)**

Update the existing import line near the top:

```python
from vergil_tooling.lib.repo_init import prompt_choice, prompt_multi_choice
```

Append these functions:

```python
def select_worktrees(
    candidates: list[Worktree],
    *,
    purpose: str,
    labels: list[str],
) -> list[Worktree]:
    """Choose one or more candidate worktrees via a checkbox-style menu.

    A single candidate is returned without prompting. With several, a TTY is
    required and a multi-select menu (numbers or 'all') is shown. ``labels``
    parallels ``candidates`` one-to-one.
    """
    if not candidates:
        msg = "select_worktrees requires at least one candidate"
        raise ValueError(msg)
    if len(candidates) == 1:
        return [candidates[0]]
    require_tty(purpose)
    return [candidates[i] for i in prompt_multi_choice(purpose, labels)]


def match_worktrees(candidates: list[Worktree], tokens: list[str]) -> list[Worktree]:
    """Resolve *tokens* (issue numbers or worktree dir names) to worktrees.

    Each token matches a candidate by directory name (``wt.path.name``) or by
    the issue number in a canonical ``issue-<N>-<slug>`` name. Result order
    follows *tokens*. Unmatched or ambiguous tokens raise ``ValueError``
    naming them — never a silent skip.
    """
    by_name = {wt.path.name: wt for wt in candidates}
    by_issue: dict[str, list[Worktree]] = {}
    for wt in candidates:
        name = wt.path.name
        if name.startswith("issue-") and len(name.split("-", 2)) >= 2:
            by_issue.setdefault(name.split("-", 2)[1], []).append(wt)

    selected: list[Worktree] = []
    unmatched: list[str] = []
    ambiguous: list[str] = []
    for raw in tokens:
        tok = raw.strip()
        if tok in by_name:
            selected.append(by_name[tok])
        elif tok in by_issue and len(by_issue[tok]) == 1:
            selected.append(by_issue[tok][0])
        elif tok in by_issue:
            ambiguous.append(tok)
        else:
            unmatched.append(tok)

    if unmatched or ambiguous:
        parts = []
        if unmatched:
            parts.append(f"no ready worktree matches: {', '.join(unmatched)}")
        if ambiguous:
            parts.append(f"ambiguous (multiple worktrees): {', '.join(ambiguous)}")
        raise ValueError("; ".join(parts))
    return selected
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_worktrees.py -v -k "select_worktrees or match_worktrees"`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/worktrees.py tests/vergil_tooling/test_worktrees.py
vrg-commit --type feat --scope worktrees --message "add multi-select and token matching (#1673)" --body "select_worktrees (checkbox multi-select) and match_worktrees (issue number or dir name, fail on unmatched/ambiguous) for batch selection. Ref #1673"
```

---

## Task 5: `worktrees.rebase_onto` (lazy rebase helper)

**Files:**
- Modify: `src/vergil_tooling/lib/worktrees.py` (add `rebase_onto`)
- Test: `tests/vergil_tooling/test_worktrees.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/vergil_tooling/test_worktrees.py
import subprocess

from vergil_tooling.lib.worktrees import rebase_onto


def test_rebase_onto_fetches_then_rebases() -> None:
    wt = _wt("issue-1-a", "feature/1-a")
    with patch(_WT_MOD + ".git.run") as run:
        rebase_onto(wt, "develop")
    assert run.call_args_list[0].args == ("-C", str(wt.path), "fetch", "origin", "develop")
    assert run.call_args_list[1].args == ("-C", str(wt.path), "rebase", "origin/develop")


def test_rebase_onto_propagates_conflict() -> None:
    wt = _wt("issue-1-a", "feature/1-a")
    err = subprocess.CalledProcessError(1, ["git", "rebase"])
    with (
        patch(_WT_MOD + ".git.run", side_effect=[None, err]),
        pytest.raises(subprocess.CalledProcessError),
    ):
        rebase_onto(wt, "develop")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_worktrees.py -v -k rebase_onto`
Expected: FAIL — `ImportError: cannot import name 'rebase_onto'`.

- [ ] **Step 3: Write implementation (add to `worktrees.py`)**

```python
def rebase_onto(worktree: Worktree, base: str) -> None:
    """Fetch *base* from origin and rebase *worktree*'s branch onto it.

    Run via ``git -C`` so the batch orchestrator can process each worktree
    without changing the process CWD. This is the step that makes a batch's
    CI gate run exactly once: rebasing onto the current ``develop`` before
    the PR opens means the gate runs against the final state and the later
    merge is not ``BEHIND``. A rebase conflict raises
    ``subprocess.CalledProcessError`` for the caller to convert to a
    ``BatchAbort``.
    """
    git.run("-C", str(worktree.path), "fetch", "origin", base)
    git.run("-C", str(worktree.path), "rebase", f"origin/{base}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_worktrees.py -v -k rebase_onto`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/worktrees.py tests/vergil_tooling/test_worktrees.py
vrg-commit --type feat --scope worktrees --message "add rebase_onto helper (#1673)" --body "Fetch origin/<base> and rebase a worktree's branch onto it via git -C, so a batch can rebase each item before submit (the zero-waste-CI mechanism). Ref #1673"
```

---

## Task 6: `vrg-finalize-pr --skip-post-checks`

Lets a batch finalize each item (merge + cleanup) while deferring container
validation, the CD check, and any release to the end of the batch.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py` (`parse_args`, `build_stages`, `main`)
- Test: `tests/vergil_tooling/test_vrg_finalize_pr.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/vergil_tooling/test_vrg_finalize_pr.py
from vergil_tooling.bin.vrg_finalize_pr import build_stages, parse_args


def test_skip_post_checks_drops_validation_and_cd() -> None:
    names = {s.name for s in build_stages(include_pr=True, include_post_checks=False)}
    assert "validation" not in names
    assert "cd-check" not in names
    assert {"provenance", "merge", "cleanup"} <= names


def test_default_keeps_post_checks() -> None:
    names = {s.name for s in build_stages(include_pr=True, include_post_checks=True)}
    assert {"validation", "cd-check"} <= names


def test_skip_post_checks_flag_parses() -> None:
    assert parse_args(["123", "--skip-post-checks"]).skip_post_checks is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v -k "skip_post_checks or post_checks"`
Expected: FAIL — `TypeError: build_stages() got an unexpected keyword argument 'include_post_checks'` / `AttributeError: ... 'skip_post_checks'`.

- [ ] **Step 3: Implement**

In `parse_args`, add the flag (after `--install`):

```python
    parser.add_argument(
        "--skip-post-checks",
        action="store_true",
        help="Skip the post-merge validation and CD-status stages (and any "
        "release chain). Used by the batch orchestrator, which runs those "
        "once at the end of the batch (issue #1673).",
    )
```

Replace `build_stages` with:

```python
def build_stages(*, include_pr: bool, include_post_checks: bool = True) -> tuple[Stage, ...]:
    """Assemble the pipeline for the resolved mode.

    provenance/merge run only when a PR was given or inferred; cleanup always
    runs. validation and cd-check run unless *include_post_checks* is False
    (the batch path defers them to one end-of-batch run, issue #1673); they
    are fail_defer so a validation failure still surfaces the CD status.
    """
    stages: list[Stage] = []
    if include_pr:
        stages.append(Stage("provenance", _stage_provenance, "fail_fast"))
        stages.append(Stage("merge", _stage_merge, "fail_fast"))
    stages.append(Stage("cleanup", _stage_cleanup, "fail_fast"))
    if include_post_checks:
        stages.append(Stage("validation", _stage_validation, "fail_defer"))
        stages.append(Stage("cd-check", _stage_cd_check, "fail_defer"))
    return tuple(stages)
```

In `main`, thread the flag into the pipeline build and suppress the release
chain when post-checks are skipped. Replace the `run_pipeline` call's
`build_stages(...)` argument and guard the release:

```python
    rc = progress.run_pipeline(
        ctx,
        build_stages(
            include_pr=args.pr is not None,
            include_post_checks=not args.skip_post_checks,
        ),
        command="vrg-finalize-pr",
        label="vrg-finalize-pr",
        args=args,
        repo_root=root,
    )

    # --release cascade (issue #1634): never chains when post-checks are
    # skipped — release is the batch orchestrator's single end-of-batch step.
    if rc != 0 or not args.release or args.skip_post_checks:
        return rc
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v`
Expected: PASS (new + existing tests).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type feat --scope finalize-pr --message "add --skip-post-checks (#1673)" --body "Skip validation/cd-check stages and the release chain so a batch can finalize each item and run those once at the end. Ref #1673"
```

---

## Task 7: `vrg-finalize-pr` batch mode (comma-list / `--all`)

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_finalize_pr.py` (`parse_args`, `main`; add helpers `_resolve_open_prs`, `_run_finalize_batch`)
- Test: `tests/vergil_tooling/test_vrg_finalize_pr.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/vergil_tooling/test_vrg_finalize_pr.py
from unittest.mock import MagicMock, patch

from vergil_tooling.bin.vrg_finalize_pr import _parse_pr_list, _run_finalize_batch

_FIN_MOD = "vergil_tooling.bin.vrg_finalize_pr"


def test_parse_pr_list_splits_and_trims() -> None:
    assert _parse_pr_list("123, 124 ,125") == ["123", "124", "125"]
    assert _parse_pr_list("123") == ["123"]


def test_finalize_batch_runs_each_item_then_release_once() -> None:
    runs: list[tuple[str, ...]] = []

    def fake_run(cmd, **_kwargs):
        runs.append(tuple(cmd))
        return MagicMock(returncode=0)

    with (
        patch(_FIN_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_FIN_MOD + ".confirm", return_value=True),
    ):
        rc = _run_finalize_batch(
            ["123", "124"],
            root=Path("/repo"),
            release=True,
            install=False,
            assume_yes=True,
        )
    assert rc == 0
    # two per-item finalizes with --skip-post-checks
    assert ("vrg-finalize-pr", "123", "--skip-post-checks") in runs
    assert ("vrg-finalize-pr", "124", "--skip-post-checks") in runs
    # one end-of-batch validation cleanup and one release
    assert ("vrg-finalize-pr", "--cleanup-only") in runs
    assert ("vrg-release",) in runs


def test_finalize_batch_stops_on_item_failure_no_release() -> None:
    def fake_run(cmd, **_kwargs):
        rc = 1 if "123" in cmd else 0
        return MagicMock(returncode=rc)

    with (
        patch(_FIN_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_FIN_MOD + ".confirm", return_value=True),
    ):
        rc = _run_finalize_batch(
            ["123", "124"],
            root=Path("/repo"),
            release=True,
            install=False,
            assume_yes=True,
        )
    assert rc == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v -k "pr_list or finalize_batch"`
Expected: FAIL — `ImportError: cannot import name '_parse_pr_list'`.

- [ ] **Step 3: Implement**

Add the `--all` flag in `parse_args` (inside the mutually-exclusive `target`
group, alongside `pr` and `--cleanup-only`):

```python
    target.add_argument(
        "--all",
        dest="all_prs",
        action="store_true",
        help="Finalize every open PR found in .worktrees/ as a serial batch "
        "(issue #1673).",
    )
```

Add helpers near the top of the module (after the imports / `_CD_WORKFLOW_NAME`):

```python
def _parse_pr_list(value: str) -> list[str]:
    """Split a comma-separated PR argument into trimmed, non-empty tokens."""
    return [tok.strip() for tok in value.split(",") if tok.strip()]


def _resolve_open_prs(root: Path) -> list[str]:
    """Return the URLs of every open PR in canonical worktrees, branch-sorted.

    Deterministic order (by branch name) so a batch is reproducible. Skips
    worktrees with no open PR, printing why — no silent exclusions.
    """
    urls: list[str] = []
    for wt in sorted(worktrees.list_worktrees(root), key=lambda w: w.branch):
        pr = github.pr_for_branch(wt.branch)
        if pr is None:
            print(f"  {wt.path.name}: no open PR for {wt.branch} — skipping")
            continue
        urls.append(pr["url"])
    return urls
```

Add the batch runner:

```python
def _run_finalize_batch(
    prs: list[str],
    *,
    root: Path,
    release: bool,
    install: bool,
    assume_yes: bool,
) -> int:
    """Finalize *prs* serially, fail-fast, then validate + release once.

    Each item shells out to ``vrg-finalize-pr <pr> --skip-post-checks`` (merge
    + cleanup, no validation). On full success, one end-of-batch
    ``vrg-finalize-pr --cleanup-only`` runs validation + the CD check, then a
    single ``vrg-release [--install]`` if requested (issue #1673).
    """

    def _finalize_item(pr: str) -> None:
        result = subprocess.run(  # noqa: S603
            ("vrg-finalize-pr", pr, "--skip-post-checks"),  # noqa: S607
            cwd=root,
            check=False,
        )
        if result.returncode != 0:
            msg = f"vrg-finalize-pr {pr} --skip-post-checks exited {result.returncode}"
            raise batch.BatchAbort(msg)

    def _validate() -> None:
        result = subprocess.run(  # noqa: S603
            ("vrg-finalize-pr", "--cleanup-only"),  # noqa: S607
            cwd=root,
            check=False,
        )
        if result.returncode != 0:
            raise batch.BatchAbort(f"end-of-batch validation exited {result.returncode}")

    def _release() -> None:
        cmd = ("vrg-release", "--install") if install else ("vrg-release",)
        result = subprocess.run(cmd, cwd=root, check=False)  # noqa: S603,S607
        if result.returncode != 0:
            raise batch.BatchAbort(f"{' '.join(cmd)} exited {result.returncode}")

    post_steps = [batch.PostStep("validation", _validate)]
    if release:
        post_steps.append(batch.PostStep("release", _release))

    plan = [f"finalize PR {pr}" for pr in prs]
    plan.append("then: validate develop once" + (", then release" if release else ""))

    report = batch.run_batch(
        prs,
        _finalize_item,
        label=lambda pr: f"PR {pr}",
        plan=plan,
        assume_yes=assume_yes,
        post_steps=post_steps,
    )
    print(batch.format_report(report))
    return 0 if report.all_merged and report.post_failure is None else 1
```

Add the imports at the top of the module:

```python
from vergil_tooling.lib.pr_workflow import batch
```

Wire batch detection into `main`, immediately after `root = git.repo_root()`
and before the single-PR inference block:

```python
    # Batch mode (issue #1673): an explicit comma-list or --all finalizes
    # several PRs serially. A single PR (or none) falls through to the
    # unchanged single-PR pipeline below.
    if args.all_prs:
        prs = _resolve_open_prs(root)
        if not prs:
            print("vrg-finalize-pr --all: no open PRs found in worktrees.")
            return 0
        return _run_finalize_batch(
            prs, root=root, release=args.release, install=args.install, assume_yes=args.yes
        )
    if args.pr is not None and "," in args.pr:
        prs = _parse_pr_list(args.pr)
        return _run_finalize_batch(
            prs, root=root, release=args.release, install=args.install, assume_yes=args.yes
        )
```

> Note: `args.all_prs` exists because the new `--all` flag uses
> `dest="all_prs"`. The single-PR `_parse_pr_list` path triggers only when a
> comma is present, so a bare `vrg-finalize-pr 123` is unchanged.

- [ ] **Step 4: Run tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_finalize_pr.py -v`
Expected: PASS (new + existing).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_finalize_pr.py tests/vergil_tooling/test_vrg_finalize_pr.py
vrg-commit --type feat --scope finalize-pr --message "add batch finalize via comma-list/--all (#1673)" --body "Serial fail-fast finalize of multiple PRs, each --skip-post-checks, then one end-of-batch validation and a single release. Ref #1673"
```

---

## Task 8: Factor `_submit_one` out of `vrg-submit-pr` template mode

Prepares submit-pr for batch reuse without changing single-PR behavior.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_submit_pr.py` (extract `_submit_one`; `_run_template_mode` calls it)
- Test: `tests/vergil_tooling/test_vrg_submit_pr.py`

- [ ] **Step 1: Write the failing test**

```python
# append to tests/vergil_tooling/test_vrg_submit_pr.py
from vergil_tooling.bin.vrg_submit_pr import _submit_one


def test_submit_one_pushes_creates_records_and_returns_url() -> None:
    fields = {
        "issue": "1673",
        "title": "Batch",
        "summary": "s",
        "notes": "",
        "linkage": "Ref",
        "base": "origin/develop",
    }
    with (
        patch(_MOD + ".submission.read_pr_fields", return_value=fields),
        patch(_MOD + ".git.current_branch", return_value="feature/1673-x"),
        patch(_MOD + "._push_branch") as push,
        patch(_MOD + "._create_pr", return_value="https://example/pull/9") as create,
        patch(_MOD + ".submission.record_submission") as record,
        patch(_MOD + ".resolve_issue_ref", return_value="#1673"),
        patch(_MOD + ".build_pr_body", return_value="BODY"),
    ):
        url = _submit_one(Path("/repo/.worktrees/issue-1673-x"), base_override=None, assume_yes=True)
    assert url == "https://example/pull/9"
    push.assert_called_once_with("feature/1673-x")
    create.assert_called_once()
    record.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py -v -k submit_one`
Expected: FAIL — `ImportError: cannot import name '_submit_one'`.

- [ ] **Step 3: Implement — extract `_submit_one`, have `_run_template_mode` call it**

Add `_submit_one` (it is the body of today's `_run_template_mode` from
`read_pr_fields` through `record_submission`, minus the cascade, returning the
URL; the per-PR confirm is governed by `assume_yes`):

```python
def _submit_one(worktree_root: Path, *, base_override: str | None, assume_yes: bool) -> str:
    """Read the worktree's PR fields, push, create the PR, record it, return URL.

    Raises ``SystemExit`` (via the field readers) on a not-ready worktree and
    re-raises push/create failures. The per-PR "Submit this PR?" confirm is
    pre-answered when *assume_yes* — the batch path passes True so the single
    up-front batch confirm is the only gate (issue #1673).
    """
    fields = submission.read_pr_fields(worktree_root)
    issue_ref = resolve_issue_ref(fields["issue"])
    branch = git.current_branch()
    target = _target_branch(base_override, fields.get("base"))
    linkage = fields.get("linkage", "Ref")
    if linkage not in ALLOWED_LINKAGES:
        msg = (
            f"linkage '{linkage}' in the PR submission fields is not allowed; "
            f"use: {', '.join(ALLOWED_LINKAGES)}."
        )
        raise SystemExit(f"vrg-submit-pr: {msg}")
    pr_body = build_pr_body(
        summary=fields["summary"],
        linkage=linkage,
        issue_ref=issue_ref,
        notes=fields.get("notes", ""),
    )
    print("=== PR from template ===")
    print(f"Title:  {fields['title']}")
    print(f"Base:   {target}")
    print(f"Branch: {branch}")
    print(f"Issue:  {issue_ref}")
    if not confirm("\nSubmit this PR?", assume_yes=assume_yes):
        msg = "submission declined at the per-PR confirm"
        raise SystemExit(f"vrg-submit-pr: {msg}")
    print(f"Ensuring branch '{branch}' is pushed to origin...")
    _push_branch(branch)
    print("Creating PR...")
    pr_url = _create_pr(target_branch=target, title=fields["title"], pr_body=pr_body)
    submission.record_submission(worktree_root, pr_url=pr_url)
    print(f"PR created: {pr_url}")
    return pr_url
```

Then simplify the single-PR `_run_template_mode` so that, after the
already-submitted / not-ready guards and the `dry_run` branch, it delegates:

```python
    pr_url = _submit_one(root, base_override=args.base, assume_yes=args.yes)
    if args.finalize:
        rc = _chain_finalize(pr_url, release=args.release, install=args.install)
        if rc != 0:
            return rc
        _print_cascade_summary(pr_url, released=args.release, installed=args.install)
        return 0
    print(f"Done. PR URL: {pr_url}")
    _print_pr_watch(pr_url)
    return 0
```

> The `AlreadySubmittedError` / `FileNotFoundError` / `TemplateError` guards and
> the `dry_run` body preview stay in `_run_template_mode` exactly as they are
> today — only the push/create/record tail moves into `_submit_one`.

- [ ] **Step 4: Run the full submit-pr test file to verify no regression**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py -v`
Expected: PASS (new `submit_one` test + all existing tests unchanged).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_submit_pr.py tests/vergil_tooling/test_vrg_submit_pr.py
vrg-commit --type refactor --scope submit-pr --message "extract _submit_one for reuse (#1673)" --body "Factor the push/create/record tail of template mode into _submit_one so batch mode can call it per item. Single-PR behavior unchanged. Ref #1673"
```

---

## Task 9: `vrg-submit-pr` batch selection + orchestration

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_submit_pr.py` (`parse_args`; refactor `_choose_submit_worktree` → `_ready_worktrees`; add `_run_submit_batch`; wire `_run_template_mode`)
- Test: `tests/vergil_tooling/test_vrg_submit_pr.py`

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/vergil_tooling/test_vrg_submit_pr.py
from unittest.mock import MagicMock

from vergil_tooling.lib.worktrees import Worktree
from vergil_tooling.bin.vrg_submit_pr import _run_submit_batch


def _wt(name: str) -> Worktree:
    n = name.split("-")[1]
    return Worktree(path=Path(f"/repo/.worktrees/{name}"), branch=f"feature/{n}-x")


def test_submit_batch_rebases_submits_then_finalizes_each_and_releases_once() -> None:
    a, b = _wt("issue-1-a"), _wt("issue-2-b")
    finalize_calls: list[tuple[str, ...]] = []

    def fake_run(cmd, **_kwargs):
        finalize_calls.append(tuple(cmd))
        return MagicMock(returncode=0)

    with (
        patch(_MOD + ".worktrees.rebase_onto") as rebase,
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._submit_one", side_effect=["https://x/pull/1", "https://x/pull/2"]),
        patch(_MOD + ".subprocess.run", side_effect=fake_run),
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = _run_submit_batch(
            [a, b], base="develop", finalize=True, release=True, install=False, assume_yes=True
        )
    assert rc == 0
    assert rebase.call_count == 2
    assert ("vrg-finalize-pr", "https://x/pull/1", "--skip-post-checks") in finalize_calls
    assert ("vrg-finalize-pr", "https://x/pull/2", "--skip-post-checks") in finalize_calls
    assert ("vrg-finalize-pr", "--cleanup-only") in finalize_calls
    assert ("vrg-release",) in finalize_calls


def test_submit_batch_rebase_conflict_stops_batch() -> None:
    import subprocess

    a, b = _wt("issue-1-a"), _wt("issue-2-b")
    with (
        patch(_MOD + ".worktrees.rebase_onto", side_effect=subprocess.CalledProcessError(1, "git")),
        patch(_MOD + ".os.chdir"),
        patch(_MOD + ".git.main_worktree_root", return_value=Path("/repo")),
        patch(_MOD + "._submit_one") as submit,
        patch(_MOD + ".confirm", return_value=True),
    ):
        rc = _run_submit_batch(
            [a, b], base="develop", finalize=True, release=False, install=False, assume_yes=True
        )
    assert rc == 1
    submit.assert_not_called()  # rebase failed before submit


def test_all_and_select_flags_parse() -> None:
    assert parse_args(["--all"]).all_worktrees is True
    assert parse_args(["--select", "1,2"]).select == "1,2"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py -v -k "submit_batch or all_and_select"`
Expected: FAIL — `ImportError: cannot import name '_run_submit_batch'`.

- [ ] **Step 3: Implement**

Add imports at the top of `vrg_submit_pr.py`:

```python
from vergil_tooling.lib.pr_workflow import batch
```

Add the flags in `parse_args` (after `--install`):

```python
    parser.add_argument(
        "--all",
        dest="all_worktrees",
        action="store_true",
        help="Select every ready worktree for a batch submission (issue #1673).",
    )
    parser.add_argument(
        "--select",
        default=None,
        help="Comma-separated list of ready worktrees to batch-submit, by issue "
        "number or worktree directory name (issue #1673).",
    )
```

Refactor `_choose_submit_worktree` so the ready-list gathering is reusable.
Extract a `_ready_worktrees` function that returns the `(Worktree, fields)`
pairs and raises the same "nothing submittable" `SystemExit` as today; keep
`_choose_submit_worktree` as the single-select wrapper:

```python
def _ready_worktrees(root: Path) -> list[tuple[worktrees.Worktree, dict[str, str]]]:
    """Return submittable ``(worktree, fields)`` pairs, or SystemExit if none.

    Same classification (ready / in-flight / not-ready) and same
    no-submittable-worktrees error as the single-select path; shared by the
    single picker and the batch selector (issue #1673).
    """
    ready: list[tuple[worktrees.Worktree, dict[str, str]]] = []
    in_flight: list[str] = []
    not_ready: list[str] = []
    for wt in worktrees.list_worktrees(root):
        try:
            fields = submission.read_pr_fields(wt.path)
        except AlreadySubmittedError as exc:
            ref = f"PR #{exc.pr_number}" if exc.pr_number is not None else "open PR"
            in_flight.append(f"{wt.path.name}: {ref} ({exc.pr_url})")
            continue
        except FileNotFoundError:
            not_ready.append(f"{wt.path.name}: no .vergil/pr-workflow.json or pr-template.yml")
            continue
        except (pr_template.TemplateError, WorkflowError) as exc:
            not_ready.append(f"{wt.path.name}: {exc}")
            continue
        ready.append((wt, fields))

    if not ready:
        lines = ["vrg-submit-pr: no submittable worktrees found."]
        if in_flight:
            lines.append("")
            lines.append("  In flight (open PR — nothing to do):")
            lines.extend(f"    {entry}" for entry in in_flight)
        if not_ready:
            lines.append("")
            lines.append("  Not ready (no submission metadata yet):")
            lines.extend(f"    {entry}" for entry in not_ready)
        if not in_flight and not not_ready:
            lines.append("  (no .worktrees/ entries exist)")
        raise SystemExit("\n".join(lines))
    return ready
```

Then rewrite `_choose_submit_worktree` to use it (single-select unchanged):

```python
def _choose_submit_worktree(root: Path) -> Path:
    """At the repo root, pick the single template-ready worktree to submit."""
    worktrees.require_tty("vrg-submit-pr from the repo root")
    ready = _ready_worktrees(root)
    if len(ready) == 1:
        wt, fields = ready[0]
        print(f"Using worktree {wt.path.name} (issue {fields['issue']}: {fields['title']})")
        return wt.path
    labels = [f"{wt.path.name} — issue {f['issue']}: {f['title']}" for wt, f in ready]
    chosen = worktrees.select_worktree(
        [wt for wt, _ in ready], purpose="Multiple submittable worktrees", labels=labels
    )
    return chosen.path
```

Add the batch selector (resolves `--all` / `--select` / interactive multi):

```python
def _select_batch_worktrees(root: Path, args: argparse.Namespace) -> list[worktrees.Worktree]:
    """Resolve the batch's worktrees from --all, --select, or a checkbox menu."""
    ready = _ready_worktrees(root)
    candidates = [wt for wt, _ in ready]
    fields_by_name = {wt.path.name: f for wt, f in ready}
    if args.all_worktrees:
        return candidates
    if args.select is not None:
        tokens = [t.strip() for t in args.select.split(",") if t.strip()]
        try:
            return worktrees.match_worktrees(candidates, tokens)
        except ValueError as exc:
            raise SystemExit(f"vrg-submit-pr --select: {exc}") from exc
    labels = [
        f"{wt.path.name} — issue {fields_by_name[wt.path.name]['issue']}: "
        f"{fields_by_name[wt.path.name]['title']}"
        for wt in candidates
    ]
    return worktrees.select_worktrees(
        candidates, purpose="Select worktrees to batch-submit", labels=labels
    )
```

Add the batch orchestration:

```python
def _run_submit_batch(
    selected: list[worktrees.Worktree],
    *,
    base: str,
    finalize: bool,
    release: bool,
    install: bool,
    assume_yes: bool,
) -> int:
    """Submit (and optionally finalize) *selected* worktrees as a serial batch.

    Per item: rebase the branch on the latest *base* (the zero-waste-CI step),
    chdir in, submit, chdir back, and — when *finalize* — shell out to
    ``vrg-finalize-pr <url> --skip-post-checks``. On full success, one
    end-of-batch validation and a single release run if requested (#1673).
    """
    main_root = git.main_worktree_root()

    def _process(wt: worktrees.Worktree) -> None:
        try:
            worktrees.rebase_onto(wt, base)
        except subprocess.CalledProcessError as exc:
            raise batch.BatchAbort(f"rebase onto origin/{base} failed: {exc}") from exc
        os.chdir(wt.path)
        try:
            pr_url = _submit_one(wt.path, base_override=base, assume_yes=True)
        finally:
            os.chdir(main_root)
        if finalize:
            result = subprocess.run(  # noqa: S603
                ("vrg-finalize-pr", pr_url, "--skip-post-checks"),  # noqa: S607
                cwd=main_root,
                check=False,
            )
            if result.returncode != 0:
                raise batch.BatchAbort(
                    f"vrg-finalize-pr {pr_url} --skip-post-checks exited {result.returncode}"
                )

    def _validate() -> None:
        result = subprocess.run(  # noqa: S603
            ("vrg-finalize-pr", "--cleanup-only"),  # noqa: S607
            cwd=main_root,
            check=False,
        )
        if result.returncode != 0:
            raise batch.BatchAbort(f"end-of-batch validation exited {result.returncode}")

    def _release() -> None:
        cmd = ("vrg-release", "--install") if install else ("vrg-release",)
        result = subprocess.run(cmd, cwd=main_root, check=False)  # noqa: S603,S607
        if result.returncode != 0:
            raise batch.BatchAbort(f"{' '.join(cmd)} exited {result.returncode}")

    post_steps: list[batch.PostStep] = []
    if finalize:
        post_steps.append(batch.PostStep("validation", _validate))
        if release:
            post_steps.append(batch.PostStep("release", _release))

    plan = [
        f"rebase + submit {wt.path.name}" + (" + finalize" if finalize else "")
        for wt in selected
    ]
    if finalize:
        plan.append("then: validate develop once" + (", then release" if release else ""))

    report = batch.run_batch(
        selected,
        _process,
        label=lambda wt: wt.path.name,
        plan=plan,
        assume_yes=assume_yes,
        post_steps=post_steps,
    )
    print(batch.format_report(report))
    return 0 if report.all_merged and report.post_failure is None else 1
```

Wire selection into `_run_template_mode`. Replace the current
"`if git.is_main_worktree():`" block at the top so a multi-selection routes to
the batch:

```python
    if git.is_main_worktree():
        # Batch when --all/--select is given, or interactively when >1 ready
        # worktree is picked. A single selection falls through to the
        # unchanged single-PR path below.
        if args.all_worktrees or args.select is not None:
            selected = _select_batch_worktrees(root, args)
            base = _target_branch(args.base) if args.base else "develop"
            return _run_submit_batch(
                selected,
                base=base,
                finalize=args.finalize,
                release=args.release,
                install=args.install,
                assume_yes=args.yes,
            )
        wt_path = _choose_submit_worktree(root)
        os.chdir(wt_path)
        root = wt_path
```

> Interactive multi-select (no flags, several ready worktrees) is deferred to a
> follow-up to keep this task focused: today's no-flag path uses
> `_choose_submit_worktree` (single-select). The batch entry points in this task
> are `--all` and `--select`. Add the no-flag checkbox routing only after these
> land. (Recorded so the omission is explicit, not silent.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `UV_PROJECT_ENVIRONMENT=.venv-host uv run pytest tests/vergil_tooling/test_vrg_submit_pr.py -v`
Expected: PASS (new batch tests + all existing tests).

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/bin/vrg_submit_pr.py tests/vergil_tooling/test_vrg_submit_pr.py
vrg-commit --type feat --scope submit-pr --message "add batch submit via --all/--select (#1673)" --body "Rebase-per-item, submit, finalize each --skip-post-checks, then validate and release once. Fail-fast; single up-front confirm. Ref #1673"
```

---

## Task 10: Docs + full validation gate

**Files:**
- Modify: `CLAUDE.md` (and/or `docs/`) — document the batch flags
- Verify: whole suite green

- [ ] **Step 1: Document the new flags**

Add a short subsection under the PR-submission docs noting:
- `vrg-submit-pr --all|--select <issues> [--finalize|--release|--install]` — batch submit.
- `vrg-finalize-pr <pr1,pr2,...>|--all [--release|--install]` — batch finalize.
- One confirmation up front; fail-fast; release runs once at the end.

Keep prose paraphrased and concise (match existing CLAUDE.md tone).

- [ ] **Step 2: Run the full validation suite (the real gate)**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS — lint, typecheck, tests, audit, common checks all green.

- [ ] **Step 3: Fix anything the suite flags**, re-run until green.

- [ ] **Step 4: Commit**

```bash
vrg-git add CLAUDE.md docs
vrg-commit --type docs --scope batch --message "document batch submit/finalize flags (#1673)" --body "Document --all/--select on vrg-submit-pr and the comma-list/--all batch on vrg-finalize-pr. Ref #1673"
```

---

## Self-review notes (author checklist — completed)

- **Spec coverage:** two entry points → Tasks 7 (finalize) + 9 (submit);
  shared orchestrator → Tasks 1–2; multi-select + `--all`/`--select` → Tasks
  3, 4, 9; lazy rebase → Tasks 5, 9; fail-fast + report → Task 2; release-once
  → Tasks 7, 9; deferred validation → Tasks 6, 7, 9; one-up-front-confirm
  invariant → Task 2 (`run_batch`) + `assume_yes=True` threaded in 7/9.
- **Known intentional scope cut:** interactive *no-flag* multi-select on
  `vrg-submit-pr` is deferred (Task 9 note). `--all`/`--select` are the batch
  entry points shipped here. This is called out explicitly, not silent.
- **Type consistency:** `BatchAbort`, `PostStep`, `run_batch`, `format_report`,
  `BatchReport.all_merged`, `ItemOutcome` names match across Tasks 1–2 and
  their consumers in Tasks 7 and 9. `build_stages(include_pr=, include_post_checks=)`
  matches between Task 6 and the `main` wiring. `_submit_one(worktree_root, *,
  base_override, assume_yes)` matches between Tasks 8 and 9.
