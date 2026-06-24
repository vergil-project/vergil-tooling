# vrg-worktree-status Timestamp Columns Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `LAST COMMIT` and `LAST MODIFIED` relative-age columns to `vrg-worktree-status` so each worktree's freshness is visible at a glance.

**Architecture:** Follow the file's existing data/presentation seam. The data layer (`lib/worktrees.py` + a new `lib/git.py` helper) gathers two epoch timestamps per worktree; the presentation layer (`bin/vrg_worktree_status.py`) formats them as relative ages (`3d ago`, `2h ago`) in two appended columns. Both timestamps are new `float | None` fields on `WorktreeStatus`, defaulted so the `vrg-finalize-pr` straggler-sweep path (which builds statuses via `classify_worktree`) is unaffected.

**Tech Stack:** Python 3.12+, pytest, `unittest.mock.patch`. Git access via the existing `vergil_tooling.lib.git` subprocess wrappers.

**Design doc:** `docs/specs/2026-06-24-worktree-status-timestamps-design.md`

## Global Constraints

- **Python 3.12+ syntax** — `X | None` unions, `StrEnum`; match the surrounding modules.
- **No silent failures** — never swallow an exception into a fallback value. A benign per-file `FileNotFoundError` race during the mtime walk is *skipped* (not an error); a genuine git failure must *raise*.
- **Git/GitHub via wrappers** — use `vrg-git` / `vrg-gh`, never raw `git`/`gh`. Raw `git` is denied by the permission model.
- **Commits via `vrg-commit`** — `vrg-git add <paths>` then `vrg-commit --type <t> --scope <s> --message <m>`.
- **Run everything in the dev container** — tests run via `vrg-container-run -- uv run pytest …`; final validation is `vrg-container-run -- vrg-validate` (the only validation command).
- **Column placement** — the two new columns are *appended* after `DIRTY`; existing column indices must not shift (the test constant `_WORKFLOW_COL = 4` stays valid).
- **Relative-age format** — `None → "-"`; `< 1 day → "{int(hours)}h ago"`; `>= 1 day → "{int(days)}d ago"`; future timestamps clamp to `0h ago` (never a negative age).

---

### Task 1: `git.committer_timestamp` helper

Add a wrapper returning the committer date (epoch seconds) of a worktree's `HEAD`. Using `HEAD` (not a branch name) is correct because a canonical worktree always has its branch checked out, so `HEAD` resolves to the branch tip.

**Files:**
- Modify: `src/vergil_tooling/lib/git.py` (add function after `commit_sha`, ~line 127)
- Test: `tests/vergil_tooling/test_git.py`

**Interfaces:**
- Consumes: `read_output(*args) -> str` (existing).
- Produces: `committer_timestamp(path: str | Path) -> int` — epoch seconds of `HEAD`'s committer date in the worktree at `path`. Raises `subprocess.CalledProcessError` on git failure (fail loud).

- [ ] **Step 1: Write the failing test**

In `tests/vergil_tooling/test_git.py`, add near the other `read_output`-based helper tests:

```python
def test_committer_timestamp_returns_epoch_int() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="1700000000"):
        assert git.committer_timestamp("/repo/.worktrees/issue-1-x") == 1700000000


def test_committer_timestamp_invokes_log_with_dash_c() -> None:
    with patch("vergil_tooling.lib.git.read_output", return_value="1700000000") as mock_ro:
        git.committer_timestamp("/wt")
    mock_ro.assert_called_once_with("-C", "/wt", "log", "-1", "--format=%ct", "HEAD")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_git.py::test_committer_timestamp_returns_epoch_int -v`
Expected: FAIL with `AttributeError: module 'vergil_tooling.lib.git' has no attribute 'committer_timestamp'`

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/lib/git.py`, after `commit_sha`:

```python
def committer_timestamp(path: str | Path) -> int:
    """Return the committer date (epoch seconds) of *path*'s checked-out HEAD.

    Run with ``-C`` so the caller need not change CWD. A canonical worktree
    always has its branch checked out, so ``HEAD`` is the branch tip.
    """
    return int(read_output("-C", str(path), "log", "-1", "--format=%ct", "HEAD"))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_git.py -k committer_timestamp -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree>
vrg-git add src/vergil_tooling/lib/git.py tests/vergil_tooling/test_git.py
vrg-commit --type feat --scope git --message "add committer_timestamp helper (#1856)"
```

---

### Task 2: `_newest_mtime` walk helper

Add a private helper in `lib/worktrees.py` that returns the newest filesystem mtime across a worktree's tracked + untracked-non-ignored files. `.gitignore` exclusion is delegated to `git ls-files --exclude-standard` (git's contract — not separately unit-tested); this task's tests cover *our* logic: the tracked∪untracked union, the missing-file skip, and the empty case.

**Files:**
- Modify: `src/vergil_tooling/lib/worktrees.py` (add module-level helper near `_probe_pr_workflow`, ~line 146)
- Test: `tests/vergil_tooling/test_worktrees.py`

**Interfaces:**
- Consumes: `git.read_output(*args) -> str` (existing).
- Produces: `_newest_mtime(path: Path) -> float | None` — max `st_mtime` over tracked + untracked-non-ignored files under `path`; `None` when no eligible files exist.

- [ ] **Step 1: Write the failing test**

In `tests/vergil_tooling/test_worktrees.py`, add `import os` to the imports and add `_newest_mtime` to the `from vergil_tooling.lib.worktrees import (...)` block. Then:

```python
def test_newest_mtime_takes_max_over_tracked_and_untracked(tmp_path: Path) -> None:
    tracked = tmp_path / "tracked.py"
    tracked.write_text("x")
    os.utime(tracked, (1000.0, 1000.0))
    untracked = tmp_path / "untracked.py"
    untracked.write_text("y")
    os.utime(untracked, (5000.0, 5000.0))
    # First read_output call → tracked listing; second → untracked listing.
    with patch(_MOD + ".git.read_output", side_effect=["tracked.py", "untracked.py"]):
        assert _newest_mtime(tmp_path) == 5000.0


def test_newest_mtime_skips_listed_but_missing_file(tmp_path: Path) -> None:
    present = tmp_path / "present.py"
    present.write_text("x")
    os.utime(present, (2000.0, 2000.0))
    with patch(_MOD + ".git.read_output", side_effect=["present.py\nghost.py", ""]):
        assert _newest_mtime(tmp_path) == 2000.0


def test_newest_mtime_none_when_no_files(tmp_path: Path) -> None:
    with patch(_MOD + ".git.read_output", side_effect=["", ""]):
        assert _newest_mtime(tmp_path) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -k newest_mtime -v`
Expected: FAIL with `ImportError: cannot import name '_newest_mtime'`

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/lib/worktrees.py`, add near `_probe_pr_workflow`:

```python
def _newest_mtime(path: Path) -> float | None:
    """Return the newest mtime across *path*'s tracked + untracked files.

    The file set is ``git ls-files`` (tracked) ∪ ``ls-files --others
    --exclude-standard`` (untracked, not gitignored), so ``.gitignore`` is
    honored and ``.venv`` / ``node_modules`` / build artifacts are skipped.
    A file listed but gone by the time it is stat'd (a benign race) is
    skipped, not an error. Returns ``None`` when no eligible files exist.
    """
    tracked = git.read_output("-C", str(path), "ls-files")
    untracked = git.read_output("-C", str(path), "ls-files", "--others", "--exclude-standard")
    names = [n for n in (*tracked.splitlines(), *untracked.splitlines()) if n]
    newest: float | None = None
    for name in names:
        try:
            mtime = (path / name).stat().st_mtime
        except FileNotFoundError:
            continue
        if newest is None or mtime > newest:
            newest = mtime
    return newest
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -k newest_mtime -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree>
vrg-git add src/vergil_tooling/lib/worktrees.py tests/vergil_tooling/test_worktrees.py
vrg-commit --type feat --scope worktrees --message "add _newest_mtime walk helper (#1856)"
```

---

### Task 3: WorktreeStatus fields + gather wiring

Add the two timestamp fields to `WorktreeStatus` and populate them in `gather_worktree_status` (both the normal and PR-lookup-failed return paths). Because `gather_worktree_status` now calls `git.committer_timestamp` and `_newest_mtime`, the existing `test_gather_*` tests — which patch `git.read_output` to `""` — would break (`int("")`); an autouse stub fixture neutralizes that across the module without touching each test.

**Files:**
- Modify: `src/vergil_tooling/lib/worktrees.py` (`WorktreeStatus` dataclass ~line 44; `gather_worktree_status` ~line 171)
- Test: `tests/vergil_tooling/test_worktrees.py`

**Interfaces:**
- Consumes: `git.committer_timestamp` (Task 1), `_newest_mtime` (Task 2).
- Produces: `WorktreeStatus.last_commit_ts: float | None` and `WorktreeStatus.last_modified_ts: float | None`, populated by `gather_worktree_status` (both default `None`).

- [ ] **Step 1: Write the failing test + autouse stub fixture**

In `tests/vergil_tooling/test_worktrees.py`, add an autouse fixture near the top (after `_MOD`) so existing gather tests keep working, plus a wiring test:

```python
@pytest.fixture(autouse=True)
def _stub_timestamps():
    # gather_worktree_status now calls these; default them so existing
    # gather tests (which stub git.read_output to "") are unaffected.
    with (
        patch(_MOD + ".git.committer_timestamp", return_value=1_700_000_000),
        patch(_MOD + "._newest_mtime", return_value=1_700_000_000.0),
    ):
        yield


def test_gather_populates_timestamps() -> None:
    wt = Worktree(path=Path("/repo/.worktrees/issue-7-foo"), branch="feature/7-foo")
    with (
        patch(_MOD + ".git.read_output", return_value=""),
        patch(_MOD + ".git.commits_ahead", return_value=1),
        patch(_MOD + ".github.pr_for_branch", return_value=None),
        patch(_MOD + ".github.closed_pr_for_branch", return_value=None),
        patch(_MOD + ".git.committer_timestamp", return_value=1_699_900_000),
        patch(_MOD + "._newest_mtime", return_value=1_699_999_999.0),
    ):
        status = gather_worktree_status(wt, target="develop")
    assert status.last_commit_ts == 1_699_900_000
    assert status.last_modified_ts == 1_699_999_999.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py::test_gather_populates_timestamps -v`
Expected: FAIL with `AttributeError: 'WorktreeStatus' object has no attribute 'last_commit_ts'`

- [ ] **Step 3: Write minimal implementation**

In `WorktreeStatus` (after `pr_prepared: bool = False`, ~line 58):

```python
    # Freshness signals (epoch seconds), attached by gather_worktree_status.
    # Defaulted so classify_worktree callers (the finalize sweep) are unaffected.
    last_commit_ts: float | None = None
    last_modified_ts: float | None = None
```

In `gather_worktree_status`, compute the timestamps after the `_probe_pr_workflow` line (~line 187) and inject them in the `_with_workflow` closure:

```python
    workflow_status, workflow_error, pr_prepared = _probe_pr_workflow(worktree)
    last_commit_ts = git.committer_timestamp(worktree.path)
    last_modified_ts = _newest_mtime(worktree.path)

    def _with_workflow(status: WorktreeStatus) -> WorktreeStatus:
        return replace(
            status,
            workflow_status=workflow_status,
            workflow_error=workflow_error,
            pr_prepared=pr_prepared,
            last_commit_ts=last_commit_ts,
            last_modified_ts=last_modified_ts,
        )
```

(The rest of `gather_worktree_status` is unchanged; both return paths already route through `_with_workflow`.)

- [ ] **Step 4: Run the full worktrees suite to verify pass + no regressions**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_worktrees.py -v`
Expected: PASS (all existing tests + `test_gather_populates_timestamps`)

- [ ] **Step 5: Commit**

```bash
cd <worktree>
vrg-git add src/vergil_tooling/lib/worktrees.py tests/vergil_tooling/test_worktrees.py
vrg-commit --type feat --scope worktrees --message "attach commit/modified timestamps to WorktreeStatus (#1856)"
```

---

### Task 4: `_format_age` formatter

Add the relative-age formatter to `bin/vrg_worktree_status.py`, mirroring `vrg_vm._format_age` but with an `" ago"` suffix, a `"-"` for `None`, and a future-clamp so clock skew never yields a negative age.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_worktree_status.py` (add module-level helper near `_workflow_cell`, ~line 46)
- Test: `tests/vergil_tooling/test_vrg_worktree_status.py`

**Interfaces:**
- Produces: `_format_age(ts: float | None, now: float) -> str`.

- [ ] **Step 1: Write the failing test**

In `tests/vergil_tooling/test_vrg_worktree_status.py`, add `_format_age` to the import from `vergil_tooling.bin.vrg_worktree_status`, and add:

```python
_NOW = 1_700_000_000.0


def test_format_age_hours() -> None:
    assert _format_age(_NOW - 2 * 3600, _NOW) == "2h ago"


def test_format_age_days() -> None:
    assert _format_age(_NOW - 3 * 86400, _NOW) == "3d ago"


def test_format_age_none_is_dash() -> None:
    assert _format_age(None, _NOW) == "-"


def test_format_age_future_clamps_to_zero() -> None:
    assert _format_age(_NOW + 5000, _NOW) == "0h ago"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_worktree_status.py -k format_age -v`
Expected: FAIL with `ImportError: cannot import name '_format_age'`

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/bin/vrg_worktree_status.py`, after `_workflow_cell`:

```python
def _format_age(ts: float | None, now: float) -> str:
    """Render *ts* (epoch seconds) as a relative age: '2h ago' / '3d ago'.

    ``None`` renders '-'. A future timestamp (clock skew, or a commit dated
    just ahead of *now*) clamps to '0h ago' rather than a negative age.
    """
    if ts is None:
        return "-"
    elapsed = max(0.0, now - ts)
    days = elapsed / 86400.0
    if days < 1:
        return f"{int(elapsed // 3600)}h ago"
    return f"{int(days)}d ago"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_worktree_status.py -k format_age -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
cd <worktree>
vrg-git add src/vergil_tooling/bin/vrg_worktree_status.py tests/vergil_tooling/test_vrg_worktree_status.py
vrg-commit --type feat --scope worktree-status --message "add _format_age relative-age formatter (#1856)"
```

---

### Task 5: Columns, row rendering, and `now` threading

Append the two columns, thread a single `now` into row rendering, and update the `_row` signature. The existing `_row` tests and the `_status` test helper need updating to pass `now` and the new fields.

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_worktree_status.py` (`_COLUMNS` ~line 31; `_row` ~line 54; `main` ~line 102)
- Test: `tests/vergil_tooling/test_vrg_worktree_status.py`

**Interfaces:**
- Consumes: `_format_age` (Task 4); `WorktreeStatus.last_commit_ts` / `last_modified_ts` (Task 3).
- Produces: `_row(status: WorktreeStatus, now: float) -> tuple[str, ...]` (9-tuple); `_COLUMNS` gains `"LAST COMMIT"`, `"LAST MODIFIED"` at the end.

- [ ] **Step 1: Update the `_status` helper and existing `_row` calls, then write failing tests**

In `tests/vergil_tooling/test_vrg_worktree_status.py`:

1. Extend the `_status` helper signature with the two new keyword args and pass them through:

```python
def _status(
    branch: str,
    state: WorktreeState,
    *,
    pr: int | None = None,
    ahead: int = 0,
    dirty: bool = False,
    detail: str | None = None,
    workflow_status: str | None = None,
    workflow_error: str | None = None,
    pr_prepared: bool = False,
    last_commit_ts: float | None = None,
    last_modified_ts: float | None = None,
) -> WorktreeStatus:
    wt = Worktree(path=Path(f"/repo/.worktrees/{branch.replace('/', '-')}"), branch=branch)
    return WorktreeStatus(
        worktree=wt,
        state=state,
        pr_number=pr,
        ahead=ahead,
        dirty=dirty,
        detail=detail,
        workflow_status=workflow_status,
        workflow_error=workflow_error,
        pr_prepared=pr_prepared,
        last_commit_ts=last_commit_ts,
        last_modified_ts=last_modified_ts,
    )
```

2. Update the three existing `_row(...)` calls to pass `now`:

```python
def test_row_renders_loaded_workflow_status_verbatim() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1, workflow_status="approved"), _NOW)
    assert row[_WORKFLOW_COL] == "approved"


def test_row_renders_dash_when_no_workflow_file() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1), _NOW)
    assert row[_WORKFLOW_COL] == "-"


def test_row_renders_unknown_on_workflow_read_error() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1, workflow_error="bad json"), _NOW)
    assert row[_WORKFLOW_COL] == "unknown"
```

3. Add the new column-index constants (after `_WORKFLOW_COL = 4`) and tests:

```python
_LAST_COMMIT_COL = 7
_LAST_MODIFIED_COL = 8


def test_row_renders_relative_ages() -> None:
    row = _row(
        _status(
            "feature/1-x",
            WorktreeState.NO_PR,
            ahead=1,
            last_commit_ts=_NOW - 3 * 86400,
            last_modified_ts=_NOW - 2 * 3600,
        ),
        _NOW,
    )
    assert row[_LAST_COMMIT_COL] == "3d ago"
    assert row[_LAST_MODIFIED_COL] == "2h ago"


def test_row_renders_dash_for_missing_timestamps() -> None:
    row = _row(_status("feature/1-x", WorktreeState.NO_PR, ahead=1), _NOW)
    assert row[_LAST_COMMIT_COL] == "-"
    assert row[_LAST_MODIFIED_COL] == "-"


def test_main_includes_timestamp_headers(capsys: pytest.CaptureFixture[str]) -> None:
    statuses = [_status("feature/1-a", WorktreeState.NO_PR, ahead=1)]
    with (
        patch(_MOD + ".git.repo_root", return_value=Path("/repo")),
        patch(_MOD + ".worktrees.list_worktrees", return_value=[s.worktree for s in statuses]),
        patch(_MOD + ".worktrees.gather_worktree_status", side_effect=statuses),
    ):
        rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "LAST COMMIT" in out
    assert "LAST MODIFIED" in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_worktree_status.py -k "relative_ages or timestamp_headers or missing_timestamps" -v`
Expected: FAIL — `_row()` takes 1 positional arg / `"LAST COMMIT"` not in output.

- [ ] **Step 3: Write minimal implementation**

In `src/vergil_tooling/bin/vrg_worktree_status.py`:

1. Append to `_COLUMNS`:

```python
_COLUMNS = (
    "WORKTREE",
    "BRANCH",
    "PR",
    "STATE",
    "WORKFLOW",
    "AHEAD",
    "DIRTY",
    "LAST COMMIT",
    "LAST MODIFIED",
)
```

2. Update `_row` to take `now` and emit the two age cells:

```python
def _row(status: WorktreeStatus, now: float) -> tuple[str, ...]:
    pr = f"#{status.pr_number}" if status.pr_number is not None else "-"
    return (
        status.worktree.path.name,
        status.worktree.branch,
        pr,
        status.state.value,
        _workflow_cell(status),
        str(status.ahead),
        "yes" if status.dirty else "-",
        _format_age(status.last_commit_ts, now),
        _format_age(status.last_modified_ts, now),
    )
```

3. In `main`, compute `now` once and pass it to `_row`. Add `import datetime` to the imports, then change the render line:

```python
    now = datetime.datetime.now(tz=datetime.UTC).timestamp()
    print(_render_table([_row(s, now) for s in statuses]))
```

- [ ] **Step 4: Run the full module suite to verify pass**

Run: `vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_worktree_status.py -v`
Expected: PASS (all existing tests + the new timestamp tests)

- [ ] **Step 5: Commit**

```bash
cd <worktree>
vrg-git add src/vergil_tooling/bin/vrg_worktree_status.py tests/vergil_tooling/test_vrg_worktree_status.py
vrg-commit --type feat --scope worktree-status --message "add LAST COMMIT/LAST MODIFIED age columns (#1856)"
```

---

### Task 6: Full validation + PR handoff

**Files:** none (verification + handoff only).

- [ ] **Step 1: Run the full validation pipeline**

Run: `vrg-container-run -- vrg-validate`
Expected: all checks green (lint, typecheck, tests, audit, common checks).

- [ ] **Step 2: Fix any validation findings**

If validation fails, fix inline and re-run Step 1 until green. Re-commit fixes with an appropriate `vrg-commit` invocation.

- [ ] **Step 3: Record the PR handoff (do NOT submit)**

Agents must not run `vrg-submit-pr`. Record PR metadata for the human:

```bash
cd <worktree>
vrg-pr-workflow report-ready \
  --title "vrg-worktree-status: add LAST COMMIT and LAST MODIFIED age columns" \
  --summary "Append two relative-age columns to vrg-worktree-status showing each worktree's last commit and last filesystem modification, so staleness vs. active uncommitted work is visible at a glance." \
  --notes "Data layer adds git.committer_timestamp and a gitignore-respecting _newest_mtime walk; two float|None fields on WorktreeStatus default so the finalize sweep is unaffected. Presentation appends the columns and formats via _format_age. Coordinates with issue-1855-extip-column (same _COLUMNS/_row — trivial textual conflict for whichever lands second)." \
  --linkage Ref
```

Then tell the human the branch is ready for `vrg-submit-pr`.

---

## Self-Review

**Spec coverage:**
- LAST COMMIT column → Tasks 1, 3, 5. ✓
- LAST MODIFIED (newest mtime, gitignore-respecting) → Tasks 2, 3, 5. ✓
- Relative-age format, future-clamp → Task 4. ✓
- Two `float | None` fields defaulted so the finalize sweep is unaffected → Task 3. ✓
- `now` computed once → Task 5. ✓
- Append-at-end placement, indices unchanged → Task 5 (`_WORKFLOW_COL = 4` preserved). ✓
- No-silent-failures (skip per-file race, raise on real git error) → Task 2 implementation + Global Constraints. ✓
- Tests: formatter boundary/None, mtime union/skip/empty, row rendering → Tasks 2, 4, 5. ✓
- Coordination note re: #1855 → carried into the PR handoff notes (Task 6). ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"; every code step shows complete code. ✓

**Type consistency:** `committer_timestamp -> int`, `_newest_mtime -> float | None`, `_format_age(float | None, float) -> str`, `_row(WorktreeStatus, float) -> tuple[str, ...]` are used consistently across tasks. Column indices: appended columns are 7 (LAST COMMIT) and 8 (LAST MODIFIED); `_WORKFLOW_COL = 4` unchanged. ✓

**Refinement vs. spec:** The spec mentioned `committer_timestamp` returning `None` for an unborn branch; this plan uses `HEAD` (always resolves in a checked-out worktree) and returns `int`, which is simpler and avoids swallowing errors. The `last_commit_ts` field still carries `None` as its dataclass default for the finalize-sweep path. Net behavior matches the design intent.
