# vrg-resolve-tracking-issue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a CLI tool that extracts the release tracking issue number from a merge commit on main, replacing fragile title-scanning in vergil-actions.

**Architecture:** Thin CLI tool (`vrg_resolve_tracking_issue.py`) using existing `lib/git` and `lib/github` wrappers. A new shared module (`lib/linkage.py`) extracts regex patterns currently duplicated in `vrg_pr_issue_linkage.py`. The tool parses the merge commit subject for a PR number, fetches the PR body via GitHub API, and extracts the `Ref #N` tracking issue.

**Tech Stack:** Python 3.12+ (stdlib only), pytest, argparse, re, subprocess wrappers from `vergil_tooling.lib`

---

### Task 1: Create `lib/linkage.py` with shared regex patterns

**Files:**
- Create: `src/vergil_tooling/lib/linkage.py`
- Test: `tests/vergil_tooling/test_linkage.py`

- [ ] **Step 1: Write failing tests for `extract_tracking_issue`**

Create `tests/vergil_tooling/test_linkage.py`:

```python
"""Tests for vergil_tooling.lib.linkage."""

from __future__ import annotations

import pytest

from vergil_tooling.lib.linkage import extract_tracking_issue


def test_ref_simple() -> None:
    assert extract_tracking_issue("Ref #123") == 123


def test_ref_with_colon() -> None:
    assert extract_tracking_issue("Ref: #456") == 456


def test_ref_bullet_dash() -> None:
    assert extract_tracking_issue("- Ref #789") == 789


def test_ref_bullet_star() -> None:
    assert extract_tracking_issue("* Ref #789") == 789


def test_ref_indented() -> None:
    assert extract_tracking_issue("  Ref #42") == 42


def test_ref_cross_repo() -> None:
    assert extract_tracking_issue("Ref org/repo#42") == 42


def test_ref_cross_repo_with_colon() -> None:
    assert extract_tracking_issue("Ref: org/repo#42") == 42


def test_no_match_returns_none() -> None:
    assert extract_tracking_issue("No issue reference here.") is None


def test_empty_string_returns_none() -> None:
    assert extract_tracking_issue("") is None


def test_ref_in_multiline_body() -> None:
    body = "## Summary\n\nDoes things.\n\nRef #100\n"
    assert extract_tracking_issue(body) == 100


def test_multiple_refs_raises_value_error() -> None:
    body = "Ref #100\nRef #200\n"
    with pytest.raises(ValueError, match="multiple"):
        extract_tracking_issue(body)


def test_autoclose_keyword_not_matched() -> None:
    assert extract_tracking_issue("Fixes #42") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_linkage.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.lib.linkage'`

- [ ] **Step 3: Implement `lib/linkage.py`**

Create `src/vergil_tooling/lib/linkage.py`:

```python
"""Shared issue-linkage regex patterns.

Extracted from ``vrg_pr_issue_linkage`` so both the CI gate and
``vrg-resolve-tracking-issue`` use the same patterns.
"""

from __future__ import annotations

import re

LINKAGE_RE = re.compile(
    r"^\s*[-*]?\s*Ref:?\s+"
    r"([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#([0-9]+)",
    re.MULTILINE,
)

AUTOCLOSE_RE = re.compile(
    r"^\s*[-*]?\s*(close[sd]?|fix(?:e[sd])?|resolve[sd]?):?\s+"
    r"([a-zA-Z0-9._-]+/[a-zA-Z0-9._-]+)?#[0-9]+",
    re.MULTILINE | re.IGNORECASE,
)


def extract_tracking_issue(text: str) -> int | None:
    """Return the tracking issue number from a ``Ref #N`` match.

    Raises ``ValueError`` if multiple ``Ref`` lines are found.
    """
    matches = LINKAGE_RE.findall(text)
    if not matches:
        return None
    if len(matches) > 1:
        msg = f"multiple tracking issue references found ({len(matches)})"
        raise ValueError(msg)
    return int(matches[0][1])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_linkage.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/lib/linkage.py tests/vergil_tooling/test_linkage.py
```

Commit message: `feat(linkage): add shared issue-linkage regex module`

---

### Task 2: Refactor `vrg_pr_issue_linkage.py` to use shared module

**Files:**
- Modify: `src/vergil_tooling/bin/vrg_pr_issue_linkage.py`
- Verify: `tests/vergil_tooling/test_vrg_pr_issue_linkage.py` (existing, unchanged)

- [ ] **Step 1: Run existing tests to confirm baseline**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_pr_issue_linkage.py -v`
Expected: All PASS

- [ ] **Step 2: Refactor `vrg_pr_issue_linkage.py` to import from `lib/linkage`**

Replace the local regex definitions with imports. The full file should become:

```python
"""Check that a pull request body includes primary issue linkage.

Reads the GitHub event payload from ``GITHUB_EVENT_PATH`` and validates
that the PR body contains ``Ref`` followed by an issue reference.
Auto-close keywords (Fixes, Closes, Resolves and variants) are
rejected — issues must remain open until post-merge workflows succeed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from vergil_tooling.lib.linkage import AUTOCLOSE_RE, LINKAGE_RE


def main(argv: list[str] | None = None) -> int:  # noqa: ARG001
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")

    if not event_path:
        print("ERROR: GITHUB_EVENT_PATH is not set.", file=sys.stderr)
        return 2

    event_file = Path(event_path)
    if not event_file.is_file():
        print(f"ERROR: event payload not found at {event_path}", file=sys.stderr)
        return 2

    with event_file.open(encoding="utf-8") as f:
        event = json.load(f)

    pr_body: str = event.get("pull_request", {}).get("body", "") or ""

    if not pr_body:
        print(
            "ERROR: pull request body is empty; issue linkage is required.",
            file=sys.stderr,
        )
        return 1

    if AUTOCLOSE_RE.search(pr_body):
        print(
            "ERROR: pull request body contains a GitHub auto-close keyword "
            "(close/fix/resolve). Use 'Ref #N' instead. "
            "Issues must remain open until post-merge workflows succeed.",
            file=sys.stderr,
        )
        return 1

    if not LINKAGE_RE.search(pr_body):
        print(
            "ERROR: pull request body must include primary issue linkage "
            "(Ref #123). Cross-repo references (Ref owner/repo#123) are "
            "also accepted.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Run existing tests to verify refactor preserves behavior**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_pr_issue_linkage.py -v`
Expected: All PASS (identical results to Step 1)

- [ ] **Step 4: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/bin/vrg_pr_issue_linkage.py
```

Commit message: `refactor(pr-issue-linkage): use shared linkage module`

---

### Task 3: Implement `vrg_resolve_tracking_issue.py` CLI tool

**Files:**
- Create: `src/vergil_tooling/bin/vrg_resolve_tracking_issue.py`
- Test: `tests/vergil_tooling/test_vrg_resolve_tracking_issue.py`

- [ ] **Step 1: Write failing tests**

Create `tests/vergil_tooling/test_vrg_resolve_tracking_issue.py`:

```python
"""Tests for vergil_tooling.bin.vrg_resolve_tracking_issue."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from vergil_tooling.bin.vrg_resolve_tracking_issue import main, parse_args

_MOD = "vergil_tooling.bin.vrg_resolve_tracking_issue"


def test_parse_args_defaults() -> None:
    args = parse_args([])
    assert args.commit == "HEAD"


def test_parse_args_custom_commit() -> None:
    args = parse_args(["--commit", "abc123"])
    assert args.commit == "abc123"


def test_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_json",
            return_value={"body": "## Summary\n\nRef #100\n"},
        ),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "100"


def test_commit_arg_forwarded() -> None:
    with (
        patch(f"{_MOD}.git.read_output", return_value="Merge pull request #42 from org/release/1.0.0") as mock_git,
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": "Ref #100\n"}),
    ):
        main(["--commit", "abc123"])
    mock_git.assert_called_once_with("log", "-1", "--format=%s", "abc123")


def test_not_a_merge_commit(capsys: pytest.CaptureFixture[str]) -> None:
    with patch(f"{_MOD}.git.read_output", return_value="feat: add widget"):
        result = main([])
    assert result == 1
    assert "not a merge commit" in capsys.readouterr().err


def test_empty_pr_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": ""}),
    ):
        result = main([])
    assert result == 1
    assert "has no body" in capsys.readouterr().err


def test_null_pr_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", return_value={"body": None}),
    ):
        result = main([])
    assert result == 1
    assert "has no body" in capsys.readouterr().err


def test_no_ref_in_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_json",
            return_value={"body": "No linkage here.\n"},
        ),
    ):
        result = main([])
    assert result == 1
    assert "no tracking issue linkage" in capsys.readouterr().err


def test_multiple_refs_in_body(capsys: pytest.CaptureFixture[str]) -> None:
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(
            f"{_MOD}.github.read_json",
            return_value={"body": "Ref #100\nRef #200\n"},
        ),
    ):
        result = main([])
    assert result == 1
    assert "multiple tracking issue references" in capsys.readouterr().err


def test_gh_api_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(
        returncode=1, cmd=["gh", "api"], stderr="Not Found"
    )
    with (
        patch(
            f"{_MOD}.git.read_output",
            return_value="Merge pull request #42 from org/release/1.0.0",
        ),
        patch(f"{_MOD}.github.current_repo", return_value="org/repo"),
        patch(f"{_MOD}.github.read_json", side_effect=err),
    ):
        result = main([])
    assert result == 2
    assert "failed to" in capsys.readouterr().err.lower()


def test_git_failure(capsys: pytest.CaptureFixture[str]) -> None:
    err = subprocess.CalledProcessError(
        returncode=128, cmd=["git", "log"], stderr="bad revision"
    )
    with patch(f"{_MOD}.git.read_output", side_effect=err):
        result = main([])
    assert result == 2
    assert "failed to" in capsys.readouterr().err.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_resolve_tracking_issue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'vergil_tooling.bin.vrg_resolve_tracking_issue'`

- [ ] **Step 3: Implement `vrg_resolve_tracking_issue.py`**

Create `src/vergil_tooling/bin/vrg_resolve_tracking_issue.py`:

```python
"""Extract the release tracking issue number from a merge commit.

Given a merge commit on main (typically HEAD in a CD workflow), this
tool extracts the PR number from the commit subject, reads the PR
body via the GitHub API, and prints the tracking issue number found
in the ``Ref #N`` linkage pattern.

Consumed by the ``version-bump-pr`` composite action in vergil-actions.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

from vergil_tooling.lib import git, github
from vergil_tooling.lib.linkage import extract_tracking_issue

_MERGE_PR_RE = re.compile(r"^Merge pull request #(\d+) from ")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Extract the tracking issue number from a merge commit.",
    )
    parser.add_argument(
        "--commit",
        default="HEAD",
        help="Merge commit to inspect (default: HEAD)",
    )
    return parser.parse_args(argv)


def _extract_pr_number(subject: str) -> int | None:
    m = _MERGE_PR_RE.match(subject)
    return int(m.group(1)) if m else None


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        subject = git.read_output("log", "-1", "--format=%s", args.commit)
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: failed to read commit {args.commit}: {exc}",
            file=sys.stderr,
        )
        return 2

    pr_num = _extract_pr_number(subject)
    if pr_num is None:
        print(
            f"ERROR: commit {args.commit} is not a merge commit "
            "(expected 'Merge pull request #N from ...' "
            "— squash and rebase merges are not supported)",
            file=sys.stderr,
        )
        return 1

    try:
        repo = github.current_repo()
        pr_data = github.read_json("api", f"repos/{repo}/pulls/{pr_num}")
    except subprocess.CalledProcessError as exc:
        print(
            f"ERROR: failed to fetch PR #{pr_num}: {exc}",
            file=sys.stderr,
        )
        return 2

    body: str = pr_data.get("body", "") or ""  # type: ignore[union-attr]
    if not body:
        print(f"ERROR: PR #{pr_num} has no body", file=sys.stderr)
        return 1

    try:
        issue_num = extract_tracking_issue(body)
    except ValueError as exc:
        print(
            f"ERROR: PR #{pr_num} body has {exc}",
            file=sys.stderr,
        )
        return 1

    if issue_num is None:
        print(
            f"ERROR: PR #{pr_num} body has no tracking issue linkage "
            "(expected 'Ref #N')",
            file=sys.stderr,
        )
        return 1

    print(issue_num)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_resolve_tracking_issue.py -v`
Expected: All PASS

- [ ] **Step 5: Commit**

```bash
cd <worktree> && vrg-git add src/vergil_tooling/bin/vrg_resolve_tracking_issue.py tests/vergil_tooling/test_vrg_resolve_tracking_issue.py
```

Commit message: `feat(resolve-tracking-issue): add CLI tool to extract tracking issue from merge commit`

---

### Task 4: Register console script and add integration test

**Files:**
- Modify: `pyproject.toml:14-35` (add console script entry)
- Modify: `tests/vergil_tooling/test_vrg_resolve_tracking_issue.py` (add integration test)

- [ ] **Step 1: Add console script registration to `pyproject.toml`**

Add the following line in alphabetical order in the `[project.scripts]` section, between `vrg-repo-profile` and `vrg-scorecard`:

```toml
vrg-resolve-tracking-issue = "vergil_tooling.bin.vrg_resolve_tracking_issue:main"
```

- [ ] **Step 2: Write fixture-based integration test**

Append to `tests/vergil_tooling/test_vrg_resolve_tracking_issue.py`:

```python
def test_integration_fixture_merge_commit(capsys: pytest.CaptureFixture[str]) -> None:
    """Full main() with fixture data matching a real merge commit pattern."""
    commit_subject = (
        "Merge pull request #856 from vergil-project/release/2.0.18"
    )
    pr_body = (
        "## Release 2.0.18\n\n"
        "### Changes\n\n"
        "- fix(repo-config): skip local checks when --repo targets a different repository\n\n"
        "Ref #830\n"
    )
    with (
        patch(f"{_MOD}.git.read_output", return_value=commit_subject),
        patch(f"{_MOD}.github.current_repo", return_value="vergil-project/vergil-tooling"),
        patch(f"{_MOD}.github.read_json", return_value={"body": pr_body}),
    ):
        result = main([])
    assert result == 0
    assert capsys.readouterr().out.strip() == "830"
```

- [ ] **Step 3: Run all tests for this tool**

Run: `cd <worktree> && vrg-container-run -- uv run pytest tests/vergil_tooling/test_vrg_resolve_tracking_issue.py tests/vergil_tooling/test_linkage.py -v`
Expected: All PASS

- [ ] **Step 4: Commit**

```bash
cd <worktree> && vrg-git add pyproject.toml tests/vergil_tooling/test_vrg_resolve_tracking_issue.py
```

Commit message: `feat(resolve-tracking-issue): register console script and add integration test`

---

### Task 5: Run full validation and update documentation

**Files:**
- Modify: `CLAUDE.md` (add tool to CLI tool list)

- [ ] **Step 1: Run full validation**

Run: `cd <worktree> && vrg-container-run -- uv run vrg-validate`
Expected: All checks pass

- [ ] **Step 2: Add tool to CLAUDE.md CLI tool list**

In the `### Python Package (src/vergil_tooling/)` section of `CLAUDE.md`, add the following entry in alphabetical order between `vrg-prepare-release` and `vrg-submit-pr`:

```markdown
- **`vrg-resolve-tracking-issue`** — Extract tracking issue number from a merge commit's PR linkage
```

- [ ] **Step 3: Run full validation again**

Run: `cd <worktree> && vrg-container-run -- uv run vrg-validate`
Expected: All checks pass

- [ ] **Step 4: Commit**

```bash
cd <worktree> && vrg-git add CLAUDE.md
```

Commit message: `docs: add vrg-resolve-tracking-issue to CLI tool list`
