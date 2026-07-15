"""GitHubTransport relay over ``refs/vergil/pr-workflow/<branch>`` (#2366).

Exercised against a throwaway local bare remote + clone, so no network or real
GitHub App is involved. ``GitHubTransport`` shells out through ``lib/git``,
which runs git in the process CWD; the fixture ``chdir``s into the clone.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.pr_workflow.github_transport import (
    GitHubTransport,
    list_relay_branches,
)
from vergil_tooling.lib.pr_workflow.state import WorkflowState

if TYPE_CHECKING:
    from pathlib import Path

_BRANCH = "feature/2366-x"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(  # noqa: S603
        ("git", *args),  # noqa: S607
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A clone wired to a bare origin, with ``develop`` pushed and CWD set to it."""
    remote = tmp_path / "remote.git"
    clone = tmp_path / "clone"
    _git("init", "--bare", str(remote), cwd=tmp_path)
    _git("init", str(clone), cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=clone)
    _git("config", "user.email", "test@example.com", cwd=clone)
    _git("remote", "add", "origin", str(remote), cwd=clone)
    (clone / "README.md").write_text("hi\n")
    _git("add", ".", cwd=clone)
    _git("commit", "-m", "init", cwd=clone)
    _git("branch", "-M", "develop", cwd=clone)
    _git("push", "origin", "develop", cwd=clone)
    _git("fetch", "origin", cwd=clone)
    monkeypatch.chdir(clone)
    return clone


def _state(head: str = "bbb") -> WorkflowState:
    return WorkflowState(
        issue="2366",
        branch=_BRANCH,
        base="origin/develop",
        status="ready",
        created_at="2026-07-14T00:00:00Z",
        updated_at="2026-07-14T00:00:00Z",
        git={"base_sha": "aaa", "head_sha": head},
    )


def test_ref_derives_from_branch() -> None:
    transport = GitHubTransport("feature/9-y")
    assert transport.ref == "refs/vergil/pr-workflow/feature/9-y"


def test_read_returns_none_when_ref_absent(clone: Path) -> None:
    assert GitHubTransport(_BRANCH).read() is None


def test_write_then_read_round_trips(clone: Path) -> None:
    transport = GitHubTransport(_BRANCH)
    transport.write(_state())
    assert transport.read() == _state()


def test_write_is_a_pure_ref_write_leaving_head_index_worktree_untouched(
    clone: Path,
) -> None:
    """The freeze-neutral invariant: write() must not run ``git commit`` or
    otherwise mutate HEAD, the index, or the working tree."""
    head_before = _git("rev-parse", "HEAD", cwd=clone)
    status_before = _git("status", "--porcelain", cwd=clone)

    GitHubTransport(_BRANCH).write(_state())

    assert _git("rev-parse", "HEAD", cwd=clone) == head_before
    assert _git("status", "--porcelain", cwd=clone) == status_before
    # And the ref landed on the remote — the write really happened, out-of-band.
    assert _git("ls-remote", "origin", GitHubTransport(_BRANCH).ref, cwd=clone)


def test_write_force_overwrites_existing_ref(clone: Path) -> None:
    transport = GitHubTransport(_BRANCH)
    transport.write(_state(head="first"))
    transport.write(_state(head="second"))
    read = transport.read()
    assert read is not None
    assert read.git["head_sha"] == "second"


def test_delete_removes_the_ref(clone: Path) -> None:
    transport = GitHubTransport(_BRANCH)
    transport.write(_state())
    transport.delete()
    assert transport.read() is None


def test_delete_is_a_noop_when_ref_absent(clone: Path) -> None:
    # No ref was ever written; delete must not raise.
    GitHubTransport(_BRANCH).delete()
    assert GitHubTransport(_BRANCH).read() is None


def test_list_relay_branches_returns_branches_with_refs(clone: Path) -> None:
    # The ``*`` glob spans slashes, so nested branch names round-trip whole.
    GitHubTransport("feature/1-a").write(_state())
    GitHubTransport("feature/2-b").write(_state())
    assert sorted(list_relay_branches()) == ["feature/1-a", "feature/2-b"]


def test_list_relay_branches_empty_when_none(clone: Path) -> None:
    assert list_relay_branches() == []


def test_list_relay_branches_ignores_malformed_lines() -> None:
    output = "sha\trefs/vergil/pr-workflow/feature/9-y\nnot-a-ref-line"
    with patch(
        "vergil_tooling.lib.pr_workflow.github_transport.git.read_output",
        return_value=output,
    ):
        assert list_relay_branches() == ["feature/9-y"]


def test_head_sha_returns_current_head(clone: Path) -> None:
    assert GitHubTransport(_BRANCH).head_sha() == _git("rev-parse", "HEAD", cwd=clone)


def test_merge_base_resolves_against_base(clone: Path) -> None:
    expected = _git("merge-base", "origin/develop", "HEAD", cwd=clone)
    assert GitHubTransport(_BRANCH).merge_base() == expected
