# Resumable Releases Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `vrg-release --resume` adopt an open `release: X.Y.Z` tracking issue and continue an interrupted release from the first incomplete stage, without bumping the version or leaving orphaned artifacts.

**Architecture:** The tracking issue is the durable state, the lock, and the human log. Its body holds a stage checklist (generated from `build_stages()`) that is the resume cursor; each stage ticks its box at the end and, on the skip path, hydrates its `ReleaseContext` outputs from reality. Issue creation moves to after read-only validation but before the first durable artifact; `close-finalize` closes the issue only when no fail-defer stage has a pending error.

**Tech Stack:** Python 3.12+, `vrg-*` tooling, `gh`/`git` via `github`/`git` wrappers, pytest with 100% coverage, ty + mypy typecheck, ruff.

**Spec:** [`docs/specs/2026-06-11-resumable-release-design.md`](./2026-06-11-resumable-release-design.md)

---

## File structure

| File | Responsibility | Phase |
|---|---|---|
| `src/vergil_tooling/lib/release/checklist.py` (new) | Render / parse / tick the delimited checklist block; version-skew guard | 1 |
| `tests/vergil_tooling/test_release_checklist.py` (new) | Unit tests for the checklist module | 1 |
| `src/vergil_tooling/lib/release/tracking.py` | Add checklist to the issue body on create; body-edit to tick a box; helper to read the body | 2 |
| `src/vergil_tooling/lib/release/preflight.py` | Split into `validate()` (read-only) and `acquire_branch()` (adopt-or-create); idempotent worktree | 2 |
| `src/vergil_tooling/lib/release/prepare.py` | Sub-step idempotency (changelog commit / push / PR); hydrate `release_pr_url` | 2 |
| `src/vergil_tooling/lib/release/bump.py` | Adopt an existing `release/post-X.Y.Z`; hydrate `bump_pr_url`/`next_version` | 2 |
| `src/vergil_tooling/lib/release/confirm.py` | Hydrate `tag`/`develop_tag`/`release_url`/CD URLs on the skip path | 2 |
| `src/vergil_tooling/lib/release/orchestrator.py` | Issue-creation ordering; per-stage tick; resume entry at first-unchecked; deferred-error-gated close | 3 |
| `src/vergil_tooling/bin/vrg_release.py` | `--resume` flag; reject `--resume` + `{minor,major}`; adopt-issue path | 3 |
| `src/vergil_tooling/lib/release/finalize.py` | `close-finalize` closes the issue only if no fail-defer error is pending | 3 |

## Phase roadmap

The feature is built in three sequential phases. Each is independently testable
and lands as its own PR; later phases depend on earlier ones.

- **Phase 1 — Checklist module (this plan).** A pure-logic library that renders,
  parses, and ticks the delimited checklist block, with the version-skew guard.
  No wiring yet; fully unit-testable in isolation.
- **Phase 2 — Idempotency & hydrate.** Make each stage safe to re-enter: split
  `preflight` into validate/acquire-branch, give `prepare` sub-step idempotency,
  `bump` adopt-existing, and add the per-stage hydrate paths that repopulate
  `ReleaseContext` from reality. Wires the checklist into `tracking` (body create
  + tick). Detailed in its own plan once Phase 1 lands.
- **Phase 3 — Resume orchestration & CLI.** Move issue creation to after
  read-only validation, add the `--resume` flag and issue adoption, enter the
  pipeline at the first unchecked box, and gate `close-finalize` on the
  deferred-error state. Detailed in its own plan once Phase 2 lands.

Phases 2 and 3 each get a full bite-sized plan document written against the
then-current code (the per-stage edits must be authored against the exact
function bodies at that time). This plan covers Phase 1 in full.

---

# Phase 1 — Checklist module

**Outcome:** `vergil_tooling.lib.release.checklist` with `render`, `upsert`,
`parse`, `first_unchecked`, `tick`, and a `ChecklistError`. The block is
delimited by HTML comment markers so edits never disturb the surrounding issue
body. The stage list is supplied by callers (Phase 3 passes the names from
`build_stages()`), keeping this module free of pipeline knowledge.

### Task 1: Module skeleton and markers

**Files:**
- Create: `src/vergil_tooling/lib/release/checklist.py`
- Test: `tests/vergil_tooling/test_release_checklist.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/vergil_tooling/test_release_checklist.py
from __future__ import annotations

import pytest

from vergil_tooling.lib.release import checklist


def test_markers_are_html_comments() -> None:
    assert checklist.BEGIN == "<!-- vrg-release:progress -->"
    assert checklist.END == "<!-- /vrg-release:progress -->"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: FAIL with `ModuleNotFoundError` / `AttributeError`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/vergil_tooling/lib/release/checklist.py
"""Render, parse, and tick the release progress checklist in an issue body.

The checklist lives in an HTML-comment-delimited block so writes never disturb
the human-written parts of the tracking-issue body. The block is the resume
cursor for vrg-release --resume (issue #1612). Stage names are supplied by the
caller; this module has no knowledge of the pipeline.
"""

from __future__ import annotations

import re

BEGIN = "<!-- vrg-release:progress -->"
END = "<!-- /vrg-release:progress -->"


class ChecklistError(Exception):
    """The checklist block is missing, malformed, or version-skewed."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
cd .worktrees/issue-1612-resumable-release
vrg-git add src/vergil_tooling/lib/release/checklist.py tests/vergil_tooling/test_release_checklist.py
vrg-commit --type feat --scope release --message "add checklist module skeleton" --body "Markers and ChecklistError for the resumable-release progress block. Ref #1612."
```

### Task 2: `render` — build the block

**Files:**
- Modify: `src/vergil_tooling/lib/release/checklist.py`
- Test: `tests/vergil_tooling/test_release_checklist.py`

- [ ] **Step 1: Write the failing test**

```python
def test_render_unchecked_and_checked() -> None:
    block = checklist.render(["audit", "prepare"], checked={"audit"})
    assert block == (
        "<!-- vrg-release:progress -->\n"
        "- [x] audit\n"
        "- [ ] prepare\n"
        "<!-- /vrg-release:progress -->"
    )


def test_render_empty_checked_defaults_to_all_unchecked() -> None:
    block = checklist.render(["audit"])
    assert "- [ ] audit" in block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: FAIL with `AttributeError: render`.

- [ ] **Step 3: Write minimal implementation**

```python
from collections.abc import Iterable, Sequence


def render(stages: Sequence[str], checked: Iterable[str] = ()) -> str:
    """Return the delimited checklist block for *stages*.

    A stage in *checked* is rendered as ``[x]``, otherwise ``[ ]``.
    """
    done = set(checked)
    lines = [BEGIN]
    lines.extend(f"- [{'x' if s in done else ' '}] {s}" for s in stages)
    lines.append(END)
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/checklist.py tests/vergil_tooling/test_release_checklist.py
vrg-commit --type feat --scope release --message "render checklist block" --body "Ref #1612."
```

### Task 3: `parse` — read the block back

**Files:**
- Modify: `src/vergil_tooling/lib/release/checklist.py`
- Test: `tests/vergil_tooling/test_release_checklist.py`

- [ ] **Step 1: Write the failing test**

```python
def test_parse_returns_stage_state_pairs() -> None:
    body = (
        "## Release 2.1.0\n\n"
        + checklist.render(["audit", "prepare"], checked={"audit"})
        + "\n\nmore text\n"
    )
    assert checklist.parse(body) == [("audit", True), ("prepare", False)]


def test_parse_accepts_capital_x() -> None:
    body = checklist.render(["audit"]).replace("[ ] audit", "[X] audit")
    assert checklist.parse(body) == [("audit", True)]


def test_parse_raises_when_no_block() -> None:
    with pytest.raises(checklist.ChecklistError, match="no .* progress block"):
        checklist.parse("## Release 2.1.0\n")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: FAIL with `AttributeError: parse`.

- [ ] **Step 3: Write minimal implementation**

```python
_ITEM_RE = re.compile(r"^\s*-\s*\[([ xX])\]\s*(\S+)\s*$")


def _block_inner(body: str) -> str:
    if BEGIN not in body or END not in body:
        msg = "no vrg-release progress block found in issue body"
        raise ChecklistError(msg)
    start = body.index(BEGIN) + len(BEGIN)
    return body[start : body.index(END)]


def parse(body: str) -> list[tuple[str, bool]]:
    """Return ``[(stage, checked)]`` parsed from the block in *body*."""
    pairs: list[tuple[str, bool]] = []
    for line in _block_inner(body).splitlines():
        match = _ITEM_RE.match(line)
        if match:
            pairs.append((match.group(2), match.group(1).lower() == "x"))
    return pairs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/checklist.py tests/vergil_tooling/test_release_checklist.py
vrg-commit --type feat --scope release --message "parse checklist block" --body "Ref #1612."
```

### Task 4: `upsert` — insert or replace the block in a body

**Files:**
- Modify: `src/vergil_tooling/lib/release/checklist.py`
- Test: `tests/vergil_tooling/test_release_checklist.py`

- [ ] **Step 1: Write the failing test**

```python
def test_upsert_appends_when_absent() -> None:
    body = checklist.upsert("## Release 2.1.0\n", ["audit"])
    assert "## Release 2.1.0" in body
    assert checklist.parse(body) == [("audit", False)]


def test_upsert_replaces_existing_block_preserving_surroundings() -> None:
    original = "head\n\n" + checklist.render(["audit"]) + "\n\ntail\n"
    updated = checklist.upsert(original, ["audit"], checked={"audit"})
    assert updated.startswith("head")
    assert updated.rstrip().endswith("tail")
    assert checklist.parse(updated) == [("audit", True)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: FAIL with `AttributeError: upsert`.

- [ ] **Step 3: Write minimal implementation**

```python
def upsert(body: str, stages: Sequence[str], checked: Iterable[str] = ()) -> str:
    """Return *body* with the checklist block inserted or replaced.

    If a block is already present it is replaced in place; otherwise the block
    is appended after a blank line.
    """
    block = render(stages, checked)
    if BEGIN in body and END in body:
        pre = body[: body.index(BEGIN)]
        post = body[body.index(END) + len(END) :]
        return pre + block + post
    return body.rstrip() + "\n\n" + block + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/checklist.py tests/vergil_tooling/test_release_checklist.py
vrg-commit --type feat --scope release --message "upsert checklist block into a body" --body "Ref #1612."
```

### Task 5: `first_unchecked` with the version-skew guard

**Files:**
- Modify: `src/vergil_tooling/lib/release/checklist.py`
- Test: `tests/vergil_tooling/test_release_checklist.py`

- [ ] **Step 1: Write the failing test**

```python
def test_first_unchecked_returns_cursor() -> None:
    body = checklist.render(["audit", "prepare", "merge"], checked={"audit"})
    assert checklist.first_unchecked(body, ["audit", "prepare", "merge"]) == "prepare"


def test_first_unchecked_all_done_returns_none() -> None:
    stages = ["audit", "prepare"]
    body = checklist.render(stages, checked=set(stages))
    assert checklist.first_unchecked(body, stages) is None


def test_first_unchecked_skew_guard_refuses_mismatch() -> None:
    body = checklist.render(["audit", "OLD_STAGE"], checked={"audit"})
    with pytest.raises(checklist.ChecklistError, match="different .* version"):
        checklist.first_unchecked(body, ["audit", "prepare"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: FAIL with `AttributeError: first_unchecked`.

- [ ] **Step 3: Write minimal implementation**

```python
def first_unchecked(body: str, expected_stages: Sequence[str]) -> str | None:
    """Return the first unchecked stage, or None if all are checked.

    Raises ``ChecklistError`` if the block's stages do not match
    *expected_stages* — a mismatch means the checklist was written by a
    different tooling version, and resume must refuse rather than guess.
    """
    pairs = parse(body)
    names = [name for name, _ in pairs]
    if names != list(expected_stages):
        msg = (
            "release checklist was written by a different vrg-release version "
            f"(found {names}, expected {list(expected_stages)}); complete the "
            "release with the original version or finish the remaining stages "
            "manually"
        )
        raise ChecklistError(msg)
    for name, checked in pairs:
        if not checked:
            return name
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/checklist.py tests/vergil_tooling/test_release_checklist.py
vrg-commit --type feat --scope release --message "first-unchecked cursor with version-skew guard" --body "Ref #1612."
```

### Task 6: `tick` — flip one box, preserving the rest

**Files:**
- Modify: `src/vergil_tooling/lib/release/checklist.py`
- Test: `tests/vergil_tooling/test_release_checklist.py`

- [ ] **Step 1: Write the failing test**

```python
def test_tick_checks_one_box_and_keeps_others() -> None:
    body = "head\n\n" + checklist.render(["audit", "prepare"], checked={"audit"})
    updated = checklist.tick(body, "prepare")
    assert checklist.parse(updated) == [("audit", True), ("prepare", True)]
    assert updated.startswith("head")


def test_tick_is_idempotent_on_already_checked() -> None:
    body = checklist.render(["audit"], checked={"audit"})
    assert checklist.parse(checklist.tick(body, "audit")) == [("audit", True)]


def test_tick_unknown_stage_raises() -> None:
    body = checklist.render(["audit"])
    with pytest.raises(checklist.ChecklistError, match="not in .* checklist"):
        checklist.tick(body, "nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: FAIL with `AttributeError: tick`.

- [ ] **Step 3: Write minimal implementation**

```python
def tick(body: str, stage: str) -> str:
    """Return *body* with *stage*'s checkbox set to ``[x]``.

    Raises ``ChecklistError`` if *stage* is not one of the block's stages.
    """
    pairs = parse(body)
    names = [name for name, _ in pairs]
    if stage not in names:
        msg = f"stage {stage!r} is not in the release checklist"
        raise ChecklistError(msg)
    checked = {name for name, was_checked in pairs if was_checked}
    checked.add(stage)
    return upsert(body, names, checked)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/checklist.py tests/vergil_tooling/test_release_checklist.py
vrg-commit --type feat --scope release --message "tick a checklist box" --body "Ref #1612."
```

### Task 7: Refactor — consolidate the shared parse logic

`first_unchecked` and `tick` both open by re-deriving the names and the
checked-set from `parse`. Extract that once now that both exist. Pure
refactor — no behavior change, tests stay green.

**Files:**
- Modify: `src/vergil_tooling/lib/release/checklist.py`

- [ ] **Step 1: Confirm the baseline is green**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: PASS (all Task 1–6 tests).

- [ ] **Step 2: Extract `_names_and_checked` and route both callers through it**

```python
def _names_and_checked(body: str) -> tuple[list[str], set[str]]:
    """Return the block's stage names (in order) and the set of checked ones."""
    pairs = parse(body)
    names = [name for name, _ in pairs]
    checked = {name for name, was_checked in pairs if was_checked}
    return names, checked


def first_unchecked(body: str, expected_stages: Sequence[str]) -> str | None:
    """Return the first unchecked stage, or None if all are checked.

    Raises ``ChecklistError`` if the block's stages do not match
    *expected_stages* — a mismatch means the checklist was written by a
    different tooling version, and resume must refuse rather than guess.
    """
    names, checked = _names_and_checked(body)
    if names != list(expected_stages):
        msg = (
            "release checklist was written by a different vrg-release version "
            f"(found {names}, expected {list(expected_stages)}); complete the "
            "release with the original version or finish the remaining stages "
            "manually"
        )
        raise ChecklistError(msg)
    for name in names:
        if name not in checked:
            return name
    return None


def tick(body: str, stage: str) -> str:
    """Return *body* with *stage*'s checkbox set to ``[x]``.

    Raises ``ChecklistError`` if *stage* is not one of the block's stages.
    """
    names, checked = _names_and_checked(body)
    if stage not in names:
        msg = f"stage {stage!r} is not in the release checklist"
        raise ChecklistError(msg)
    checked.add(stage)
    return upsert(body, names, checked)
```

Both error messages now end with "the release checklist" phrasing —
keep them consistent.

- [ ] **Step 3: Run tests — still green (no behavior change)**

Run: `uv run python -m pytest tests/vergil_tooling/test_release_checklist.py -q`
Expected: PASS, unchanged from Step 1.

- [ ] **Step 4: Commit**

```bash
vrg-git add src/vergil_tooling/lib/release/checklist.py
vrg-commit --type refactor --scope release --message "extract shared names/checked helper in checklist" --body "Ref #1612."
```

### Task 8: Full validation and coverage

**Files:**
- (no source change)

- [ ] **Step 1: Run the full validation suite**

Run: `vrg-container-run -- vrg-validate`
Expected: PASS — including 100% coverage of `checklist.py` (every branch of
`render`, `parse`, `upsert`, `first_unchecked`, `tick` is exercised by Tasks
2–6). If coverage is short, add the missing case (e.g. `parse` on a body whose
block has a non-item line) and re-run.

- [ ] **Step 2: Commit any coverage fixups**

```bash
vrg-git add tests/vergil_tooling/test_release_checklist.py
vrg-commit --type test --scope release --message "cover remaining checklist branches" --body "Ref #1612."
```

---

## Phase 1 self-review

- **Spec coverage:** Phase 1 implements the spec's "checklist lives in a
  delimited block", "source of truth is `build_stages()`" (callers pass the
  list), "`[x]` vs `[ ]` is the only thing parsed", the version-skew guard, and
  `tick`/`first_unchecked` cursor semantics. The *wiring* of these into the
  pipeline (body create, per-stage tick, resume entry) is Phase 2/3 — out of
  scope here by design.
- **No placeholders:** every step shows complete code and exact commands.
- **Type consistency:** `render`/`upsert` take `Sequence[str]` + `Iterable[str]`;
  `parse` returns `list[tuple[str, bool]]`; `first_unchecked`/`tick` consume
  those exact shapes. `ChecklistError` is the single error type throughout.

When Phase 1 lands, the Phase 2 plan is written next against the then-current
`tracking.py`, `preflight.py`, `prepare.py`, `bump.py`, and `confirm.py`.
