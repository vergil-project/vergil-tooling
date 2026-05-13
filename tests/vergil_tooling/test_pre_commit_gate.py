"""Tests for the .githooks/pre-commit env-var gate.

The gate is bash; we drive it via subprocess and assert exit codes /
stderr. Three branches:

1. ST_COMMIT_CONTEXT=1 → exit 0 (st-commit-driven commit, admitted).
2. GIT_REFLOG_ACTION matches an admitted pattern (amend, cherry-pick,
   revert, rebase*, merge*) → exit 0 (legitimate derived-commit
   workflow).
3. Neither signal present → exit 1 with rejection message
   (raw `git commit -m "..."`).

Reference: docs/specs/host-level-tool.md "Git hooks"; plan Task 1.3.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_GATE_PATH = _REPO_ROOT / ".githooks" / "pre-commit"
_BASH_PATH = shutil.which("bash") or "/bin/bash"


def _run_gate(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """Invoke the gate with a clean env (plus PATH for bash itself)."""
    if not _GATE_PATH.exists():
        pytest.skip(
            f"gate not yet implemented at {_GATE_PATH} (RED phase)",
        )
    full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **env}
    return subprocess.run(  # noqa: S603
        [_BASH_PATH, str(_GATE_PATH)],
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_gate_admits_st_commit_context() -> None:
    result = _run_gate({"ST_COMMIT_CONTEXT": "1"})
    assert result.returncode == 0, result.stderr
    # Admits print a diagnostic so the admission is visible in commit
    # output — this branch is also the documented escape hatch and a
    # silent admit would hide manual workarounds.
    assert "admitted" in result.stderr
    assert "ST_COMMIT_CONTEXT" in result.stderr


@pytest.mark.parametrize(
    "reflog_action",
    [
        "amend",
        "cherry-pick",
        "revert",
        "rebase",
        "rebase -i",
        "rebase --continue",
        "rebase (start)",
        "merge",
        "merge develop",
    ],
)
def test_gate_admits_git_reflog_action(reflog_action: str) -> None:
    result = _run_gate({"GIT_REFLOG_ACTION": reflog_action})
    assert result.returncode == 0, result.stderr
    assert "admitted" in result.stderr
    assert reflog_action in result.stderr


def test_gate_rejects_raw_git_commit() -> None:
    result = _run_gate({})
    assert result.returncode == 1
    assert "raw 'git commit' is blocked" in result.stderr
    assert "st-commit" in result.stderr


def test_gate_rejects_unrelated_reflog_action() -> None:
    # Reflog actions outside the admitted set (e.g., the default "commit"
    # produced by raw `git commit -m "..."`) must be rejected.
    result = _run_gate({"GIT_REFLOG_ACTION": "commit"})
    assert result.returncode == 1
    assert "raw 'git commit' is blocked" in result.stderr


def test_gate_admits_when_st_commit_context_set_even_with_unrelated_reflog() -> None:
    # ST_COMMIT_CONTEXT takes precedence: if st-commit set the env var,
    # the gate admits regardless of GIT_REFLOG_ACTION value. The
    # diagnostic identifies the path taken (ST_COMMIT_CONTEXT, not
    # GIT_REFLOG_ACTION).
    result = _run_gate({"ST_COMMIT_CONTEXT": "1", "GIT_REFLOG_ACTION": "commit"})
    assert result.returncode == 0, result.stderr
    assert "ST_COMMIT_CONTEXT" in result.stderr


def test_gate_st_commit_context_other_value_does_not_admit() -> None:
    # The gate uses == "1" exactly. Other truthy-looking values must not
    # admit, to keep the contract narrow.
    result = _run_gate({"ST_COMMIT_CONTEXT": "true"})
    assert result.returncode == 1
