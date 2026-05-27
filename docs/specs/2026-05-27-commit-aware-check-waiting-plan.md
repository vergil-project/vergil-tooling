# Commit-aware check waiting — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `wait_for_checks()` commit-aware and remove `--fail-fast` so GHAS stale checks and stale check suites from previous commits no longer cause false early exits.

**Architecture:** Add `head_sha()` to `github.py` to resolve the PR's HEAD commit. Replace `_checks_registered()` with a SHA-pinned REST API query using `_run_with_retry`. Drop `--fail-fast` from the `gh pr checks --watch` call. No changes to `vrg-wait-until-green` — `wait_for_checks` resolves the SHA internally.

**Tech Stack:** Python 3.12+, `gh` CLI, GitHub REST API (`/commits/{sha}/check-runs`), `unittest.mock`

---

### Task 1: Add `head_sha()` to `github.py`

**Files:**
- Modify: `src/vergil_tooling/lib/github.py` (after `current_repo()`, around line 406)
- Test: `tests/vergil_tooling/test_github.py`

- [ ] **Step 1: Write the failing test for `head_sha()`**

Add after the existing `test_current_repo` test in `tests/vergil_tooling/test_github.py`:

```python
def test_head_sha_returns_commit_sha() -> None:
    with patch(
        "vergil_tooling.lib.github.read_output",
        return_value="abc123def456",
    ) as mock_read:
        result = github.head_sha("https://github.com/pr/1")
    assert result == "abc123def456"
    mock_read.assert_called_once_with(
        "pr", "view", "https://github.com/pr/1",
        "--json", "headRefOid", "--jq", ".headRefOid",
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/vergil_tooling/test_github.py::test_head_sha_returns_commit_sha -v`
Expected: FAIL — `module 'vergil_tooling.lib.github' has no attribute 'head_sha'`

- [ ] **Step 3: Implement `head_sha()` in `github.py`**

Add after `current_repo()` in `src/vergil_tooling/lib/github.py`:

```python
def head_sha(pr: str) -> str:
    """Return the HEAD commit SHA for a PR."""
    return read_output(
        "pr", "view", pr, "--json", "headRefOid", "--jq", ".headRefOid"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/vergil_tooling/test_github.py::test_head_sha_returns_commit_sha -v`
Expected: PASS

- [ ] **Step 5: Commit**

```
vrg-git add src/vergil_tooling/lib/github.py tests/vergil_tooling/test_github.py
vrg-commit --type feat --scope github --message "add head_sha() to resolve PR HEAD commit"
```

---

### Task 2: Replace `_checks_registered()` with SHA-pinned REST API query

**Files:**
- Modify: `src/vergil_tooling/lib/github.py:313-321`
- Test: `tests/vergil_tooling/test_github.py:245-260`

- [ ] **Step 1: Write the failing tests for the new `_checks_registered()`**

Replace the three existing `test_checks_registered_*` tests (lines 245-260) with:

```python
def test_checks_registered_returns_true_when_checks_exist() -> None:
    cp = _completed(stdout="1\n")
    with patch("vergil_tooling.lib.retry.subprocess.run", return_value=cp):
        assert github._checks_registered("owner/repo", "abc123") is True


def test_checks_registered_returns_false_when_no_checks() -> None:
    cp = _completed(stdout="0\n")
    with patch("vergil_tooling.lib.retry.subprocess.run", return_value=cp):
        assert github._checks_registered("owner/repo", "abc123") is False


def test_checks_registered_calls_correct_api_endpoint() -> None:
    cp = _completed(stdout="0\n")
    with patch("vergil_tooling.lib.retry.subprocess.run", return_value=cp) as mock_run:
        github._checks_registered("owner/repo", "abc123def456")
    args = mock_run.call_args[0][0]
    assert "repos/owner/repo/commits/abc123def456/check-runs" in args
    assert "--jq" in args
    assert ".total_count" in args
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_github.py::test_checks_registered_returns_true_when_checks_exist tests/vergil_tooling/test_github.py::test_checks_registered_returns_false_when_no_checks tests/vergil_tooling/test_github.py::test_checks_registered_calls_correct_api_endpoint -v`
Expected: FAIL — `_checks_registered() got an unexpected keyword argument` or similar signature mismatch

- [ ] **Step 3: Replace `_checks_registered()` in `github.py`**

Replace the existing `_checks_registered` function (lines 313-321) with:

```python
def _checks_registered(repo: str, sha: str) -> bool:
    """Return True if at least one check run exists for *sha*."""
    result = _run_with_retry(
        ("gh", "api", f"repos/{repo}/commits/{sha}/check-runs",  # noqa: S607
         "--jq", ".total_count"),
        check=True,
        text=True,
        capture_output=True,
    )
    return int(result.stdout.strip()) > 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_github.py::test_checks_registered_returns_true_when_checks_exist tests/vergil_tooling/test_github.py::test_checks_registered_returns_false_when_no_checks tests/vergil_tooling/test_github.py::test_checks_registered_calls_correct_api_endpoint -v`
Expected: PASS

- [ ] **Step 5: Commit**

```
vrg-git add src/vergil_tooling/lib/github.py tests/vergil_tooling/test_github.py
vrg-commit --type fix --scope github --message "replace _checks_registered with SHA-pinned REST API query"
```

---

### Task 3: Update `wait_for_checks()` to use SHA-pinned flow and drop `--fail-fast`

**Files:**
- Modify: `src/vergil_tooling/lib/github.py:324-355`
- Test: `tests/vergil_tooling/test_github.py:68-124`

- [ ] **Step 1: Write the failing tests for the updated `wait_for_checks()`**

Replace the four existing `test_wait_for_checks_*` tests (lines 68-124) with:

```python
def test_wait_for_checks_resolves_sha_and_watches() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123") as mock_sha,
        patch("vergil_tooling.lib.github._checks_registered", return_value=True),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1")
    mock_sha.assert_called_once_with("https://github.com/pr/1")
    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch"
    )


def test_wait_for_checks_polls_until_registered() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch(
            "vergil_tooling.lib.github._checks_registered",
            side_effect=[False, False, True, True],
        ),
        patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(5)
    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch"
    )


def test_wait_for_checks_passes_repo_and_sha_to_checks_registered() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch("vergil_tooling.lib.github._checks_registered", return_value=True) as mock_reg,
        patch("vergil_tooling.lib.github.run"),
    ):
        github.wait_for_checks("https://github.com/pr/1")
    mock_reg.assert_called_with("owner/repo", "abc123")


def test_wait_for_checks_raises_after_timeout_with_sha() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123def456"),
        patch("vergil_tooling.lib.github._checks_registered", return_value=False),
        patch(
            "vergil_tooling.lib.github.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ),
        patch("vergil_tooling.lib.github.time.sleep"),
        patch("vergil_tooling.lib.github.run") as mock_run,
        pytest.raises(github.GitHubAPIError, match="abc123de"),
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)
    mock_run.assert_not_called()


def test_wait_for_checks_uses_poll_interval_for_sleep() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch(
            "vergil_tooling.lib.github._checks_registered",
            side_effect=[False, True, True],
        ),
        patch("vergil_tooling.lib.github.time.sleep") as mock_sleep,
        patch("vergil_tooling.lib.github.run"),
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=10, poll_timeout=60)
    mock_sleep.assert_called_once_with(10)


def test_wait_for_checks_does_not_use_fail_fast() -> None:
    with (
        patch("vergil_tooling.lib.github.current_repo", return_value="owner/repo"),
        patch("vergil_tooling.lib.github.head_sha", return_value="abc123"),
        patch("vergil_tooling.lib.github._checks_registered", return_value=True),
        patch("vergil_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1")
    call_args = mock_run.call_args[0]
    assert "--fail-fast" not in call_args
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/vergil_tooling/test_github.py -k "wait_for_checks" -v`
Expected: FAIL — tests expect `current_repo` and `head_sha` calls that don't happen yet, and expect `--watch` without `--fail-fast`

- [ ] **Step 3: Update `wait_for_checks()` in `github.py`**

Replace the existing `wait_for_checks` function (lines 324-355) with:

```python
def wait_for_checks(
    pr: str,
    *,
    poll_interval: int = _POLL_INTERVAL_SECS,
    poll_timeout: int = _POLL_TIMEOUT_SECS,
) -> None:
    """Block until all checks on *pr* reach a terminal state.

    Resolves the PR's HEAD commit SHA and polls the GitHub REST API
    until at least one check run exists for that commit.  Then hands
    off to ``gh pr checks --watch`` which blocks until every check
    completes.

    Transient GitHub API errors (502/503/504/429) are retried
    automatically via the library-level retry wrapper.
    """
    repo = current_repo()
    sha = head_sha(pr)

    deadline = time.monotonic() + poll_timeout
    while not _checks_registered(repo, sha):
        if time.monotonic() >= deadline:
            break
        time.sleep(poll_interval)

    if not _checks_registered(repo, sha):
        cmd = ("gh", "pr", "checks", pr, "--watch")
        raise GitHubAPIError(
            1,
            cmd,
            stderr=(
                f"no checks reported for {sha[:8]} after {poll_timeout}s"
                " — GitHub may be experiencing delays"
            ),
        )

    run("pr", "checks", pr, "--watch")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/vergil_tooling/test_github.py -k "wait_for_checks" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```
vrg-git add src/vergil_tooling/lib/github.py tests/vergil_tooling/test_github.py
vrg-commit --type fix --scope github --message "make wait_for_checks commit-aware and drop fail-fast"
```

---

### Task 4: Remove unused `_NO_CHECKS_PHRASE` constant

**Files:**
- Modify: `src/vergil_tooling/lib/github.py:228`

- [ ] **Step 1: Verify `_NO_CHECKS_PHRASE` is no longer referenced**

Run: `grep -n '_NO_CHECKS_PHRASE' src/vergil_tooling/lib/github.py`
Expected: only the definition on line 228, no other references

- [ ] **Step 2: Remove the constant**

Delete line 228 from `src/vergil_tooling/lib/github.py`:

```python
_NO_CHECKS_PHRASE = "no checks reported"
```

- [ ] **Step 3: Run the full test suite to verify nothing breaks**

Run: `uv run pytest tests/vergil_tooling/test_github.py tests/vergil_tooling/test_vrg_wait_until_green.py -v`
Expected: all tests pass

- [ ] **Step 4: Commit**

```
vrg-git add src/vergil_tooling/lib/github.py
vrg-commit --type refactor --scope github --message "remove unused _NO_CHECKS_PHRASE constant"
```

---

### Task 5: Run full validation

**Files:** none (verification only)

- [ ] **Step 1: Run the full validation pipeline**

Run: `vrg-container-run -- uv run vrg-validate`
Expected: all checks pass (lint, typecheck, tests, audit)

- [ ] **Step 2: Verify `vrg-wait-until-green` tests still pass unchanged**

Run: `uv run pytest tests/vergil_tooling/test_vrg_wait_until_green.py -v`
Expected: all 13 tests pass with no modifications — the `wait_for_checks` signature is unchanged
