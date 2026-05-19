# vrg-wait-until-green merge-state awareness — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `vrg-wait-until-green` exit non-zero when CI checks pass but the PR is blocked by branch protection, instead of falsely reporting success.

**Architecture:** Add a `merge_status()` helper to `github.py` that fetches `mergeStateStatus` and `reviewDecision` in one API call. Replace the unconditional "All checks passed" in `vrg_wait_until_green.py` with a post-loop merge-state check that exits 0 only for CLEAN.

**Tech Stack:** Python 3.12+, `gh` CLI, `unittest.mock`

---

### Task 1: Add `merge_status()` helper to `github.py`

**Files:**
- Modify: `src/vergil_tooling/lib/github.py:267-277`
- Test: `tests/vergil_tooling/test_github.py`

- [ ] **Step 1: Write the failing test for `merge_status()`**

Add this test after the existing `test_merge_state_status_returns_behind` test (line 123) in `tests/vergil_tooling/test_github.py`:

```python
def test_merge_status_returns_both_fields() -> None:
    with patch(
        "vergil_tooling.lib.github.read_json",
        return_value={"mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED"},
    ):
        result = github.merge_status("https://github.com/pr/1")
    assert result == {"mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED"}


def test_merge_status_with_empty_review_decision() -> None:
    with patch(
        "vergil_tooling.lib.github.read_json",
        return_value={"mergeStateStatus": "BLOCKED", "reviewDecision": ""},
    ):
        result = github.merge_status("https://github.com/pr/1")
    assert result == {"mergeStateStatus": "BLOCKED", "reviewDecision": ""}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .worktrees/issue-806-merge-state && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::test_merge_status_returns_both_fields tests/vergil_tooling/test_github.py::test_merge_status_with_empty_review_decision -v`
Expected: FAIL — `module 'vergil_tooling.lib.github' has no attribute 'merge_status'`

- [ ] **Step 3: Implement `merge_status()` in `github.py`**

Add after the existing `merge_state_status()` function (after line 277) in `src/vergil_tooling/lib/github.py`:

```python
def merge_status(pr: str) -> dict[str, str]:
    """Return merge state and review decision for a PR.

    Single API call returning ``{"mergeStateStatus": ..., "reviewDecision": ...}``.
    """
    result = read_json(
        "pr",
        "view",
        pr,
        "--json",
        "mergeStateStatus,reviewDecision",
    )
    assert isinstance(result, dict)
    state = str(result.get("mergeStateStatus", ""))
    review = result.get("reviewDecision")
    return {"mergeStateStatus": state, "reviewDecision": str(review) if review else ""}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd .worktrees/issue-806-merge-state && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_github.py::test_merge_status_returns_both_fields tests/vergil_tooling/test_github.py::test_merge_status_with_empty_review_decision -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd .worktrees/issue-806-merge-state && vrg-commit -m "feat(github): add merge_status() helper for combined merge state and review decision query"
```

---

### Task 2: Update `vrg_wait_until_green.py` to check merge state after loop

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_wait_until_green.py:40-52`
- Test: `tests/vergil_tooling/test_vrg_wait_until_green.py`

- [ ] **Step 1: Write the failing test — CLEAN still exits 0**

Replace the existing `test_main_happy_path_not_behind` test in `tests/vergil_tooling/test_vrg_wait_until_green.py` to also mock `merge_status`:

```python
def test_main_happy_path_not_behind() -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks") as mock_wait,
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
        patch(
            f"{_MOD}.github.merge_status",
            return_value={"mergeStateStatus": "CLEAN", "reviewDecision": ""},
        ),
    ):
        result = main([_PR])
    assert result == 0
    mock_wait.assert_called_once_with(_PR)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd .worktrees/issue-806-merge-state && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_wait_until_green.py::test_main_happy_path_not_behind -v`
Expected: FAIL — `merge_status` not patched / not called in the code yet

- [ ] **Step 3: Write the failing tests — non-CLEAN states exit 1**

Replace the existing `test_main_succeeds_for_non_behind_states` with separate tests in `tests/vergil_tooling/test_vrg_wait_until_green.py`:

```python
def test_main_succeeds_only_for_clean() -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="CLEAN"),
        patch(
            f"{_MOD}.github.merge_status",
            return_value={"mergeStateStatus": "CLEAN", "reviewDecision": ""},
        ),
        patch(f"{_MOD}.github.update_branch") as mock_update,
    ):
        result = main([_PR])
    assert result == 0
    mock_update.assert_not_called()


def test_main_fails_for_blocked_with_review_required(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="BLOCKED"),
        patch(
            f"{_MOD}.github.merge_status",
            return_value={"mergeStateStatus": "BLOCKED", "reviewDecision": "REVIEW_REQUIRED"},
        ),
    ):
        result = main([_PR])
    assert result == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "REVIEW_REQUIRED" in err
    assert "branch protection" in err.lower()


def test_main_fails_for_blocked_without_actionable_review(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="BLOCKED"),
        patch(
            f"{_MOD}.github.merge_status",
            return_value={"mergeStateStatus": "BLOCKED", "reviewDecision": "APPROVED"},
        ),
    ):
        result = main([_PR])
    assert result == 1
    err = capsys.readouterr().err
    assert "BLOCKED" in err
    assert "APPROVED" not in err
    assert "branch protection" in err.lower()


def test_main_fails_for_dirty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="DIRTY"),
        patch(
            f"{_MOD}.github.merge_status",
            return_value={"mergeStateStatus": "DIRTY", "reviewDecision": ""},
        ),
    ):
        result = main([_PR])
    assert result == 1
    assert "DIRTY" in capsys.readouterr().err


def test_main_fails_for_unstable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="UNSTABLE"),
        patch(
            f"{_MOD}.github.merge_status",
            return_value={"mergeStateStatus": "UNSTABLE", "reviewDecision": ""},
        ),
    ):
        result = main([_PR])
    assert result == 1
    assert "UNSTABLE" in capsys.readouterr().err


def test_main_fails_for_unknown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(f"{_MOD}.github.mergeable", return_value="MERGEABLE"),
        patch(f"{_MOD}.github.wait_for_checks"),
        patch(f"{_MOD}.github.merge_state_status", return_value="UNKNOWN"),
        patch(
            f"{_MOD}.github.merge_status",
            return_value={"mergeStateStatus": "UNKNOWN", "reviewDecision": ""},
        ),
    ):
        result = main([_PR])
    assert result == 1
    assert "UNKNOWN" in capsys.readouterr().err
```

- [ ] **Step 4: Run the new tests to confirm they fail**

Run: `cd .worktrees/issue-806-merge-state && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_wait_until_green.py -k "blocked or dirty or unstable or unknown or succeeds_only" -v`
Expected: FAIL — the current code exits 0 for all non-BEHIND states

- [ ] **Step 5: Implement the merge-state check in `vrg_wait_until_green.py`**

Replace lines 40–52 (from `if github.merge_state_status` through `return 0`) in `src/vergil_tooling/bin/vrg_wait_until_green.py`:

```python
        if github.merge_state_status(args.pr) != "BEHIND":
            break
        updates += 1
        if updates > _MAX_BRANCH_UPDATES:
            print(
                "Branch still behind after multiple updates — giving up.",
                file=sys.stderr,
            )
            return 1
        print("Branch is behind base — updating and re-checking...")
        github.update_branch(args.pr)
    status = github.merge_status(args.pr)
    if status["mergeStateStatus"] == "CLEAN":
        print("All checks passed.")
        return 0
    state = status["mergeStateStatus"]
    print(
        f"All checks passed, but PR is not mergeable ({state}).",
        file=sys.stderr,
    )
    if state == "BLOCKED":
        review = status["reviewDecision"]
        if review in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            print(f"  Review status: {review}", file=sys.stderr)
        print("  Check branch protection settings.", file=sys.stderr)
    return 1
```

- [ ] **Step 6: Run all wait-until-green tests**

Run: `cd .worktrees/issue-806-merge-state && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_wait_until_green.py -v`
Expected: PASS for all tests

- [ ] **Step 7: Update tests that now need `merge_status` mocked**

The following existing tests call `main()` and reach the post-loop code, so they need `merge_status` patched:

- `test_main_updates_branch_when_behind` — final loop iteration exits with CLEAN
- `test_main_updates_branch_multiple_times` — final loop iteration exits with CLEAN

Update both to add the `merge_status` mock:

In `test_main_updates_branch_when_behind`, add inside the `with` block:
```python
        patch(
            f"{_MOD}.github.merge_status",
            return_value={"mergeStateStatus": "CLEAN", "reviewDecision": ""},
        ),
```

In `test_main_updates_branch_multiple_times`, add the same mock.

- [ ] **Step 8: Run full test suite to verify everything passes**

Run: `cd .worktrees/issue-806-merge-state && vrg-docker-run -- uv run pytest tests/vergil_tooling/test_vrg_wait_until_green.py -v`
Expected: All tests PASS

- [ ] **Step 9: Commit**

```bash
cd .worktrees/issue-806-merge-state && vrg-commit -m "fix(wait-until-green): exit non-zero when PR is blocked by branch protection (#806)"
```

---

### Task 3: Run full validation

**Files:** None (validation only)

- [ ] **Step 1: Run `vrg-validate`**

Run: `cd .worktrees/issue-806-merge-state && vrg-docker-run -- uv run vrg-validate`
Expected: All checks pass, 100% coverage maintained

- [ ] **Step 2: Fix any issues**

If linting, type checking, or coverage failures occur, fix them and re-run.

- [ ] **Step 3: Commit fixes if any**

```bash
cd .worktrees/issue-806-merge-state && vrg-commit -m "style: fix lint/type issues from merge-state changes"
```
