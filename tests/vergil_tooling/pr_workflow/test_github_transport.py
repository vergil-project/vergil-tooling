"""GitHubTransport relay over ``refs/vergil/pr-workflow/<branch>`` (#2366).

Exercised against a throwaway local bare remote + clone, so no network or real
GitHub App is involved. ``GitHubTransport`` shells out through ``lib/git``,
which runs git in the process CWD; the fixture ``chdir``s into the clone.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import github, trust
from vergil_tooling.lib.pr_workflow.github_transport import (
    GitHubTransport,
    list_relay_branches,
)
from vergil_tooling.lib.pr_workflow.state import WorkflowState

if TYPE_CHECKING:
    from pathlib import Path

_BRANCH = "feature/2366-x"
_REPO = "org/repo"


class _Commits:
    """A ``github.graphql`` stand-in for the layer's commit query: one author for every commit."""

    def __init__(self) -> None:
        self.user: Any = trust_stubs.HUMAN
        self.reads: list[tuple[str, str]] = []

    def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        assert query == trust._COMMIT_QUERY, "only the commit query is faked"
        self.reads.append((f"{variables['owner']}/{variables['name']}", str(variables["oid"])))
        return trust_stubs.commit_author(self.user)


@pytest.fixture(autouse=True)
def commits(monkeypatch: pytest.MonkeyPatch) -> _Commits:
    """The layer's commit reads: each relay commit has a trusted author unless a test says."""
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    monkeypatch.setattr(github, "current_repo", lambda: _REPO)
    fake = _Commits()
    monkeypatch.setattr(github, "graphql", fake)
    return fake


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


def test_write_then_read_round_trips(clone: Path, commits: _Commits) -> None:
    transport = GitHubTransport(_BRANCH)
    transport.write(_state())
    assert transport.read() == _state()
    # The payload was parsed only after its commit's author was judged.
    relay_commit = _git("ls-remote", "origin", transport.ref, cwd=clone).split()[0]
    assert commits.reads == [(_REPO, relay_commit)]


@pytest.mark.parametrize(
    ("user", "who", "cause"),
    [
        (trust_stubs.OUTSIDER, "drive-by", "untrusted"),
        # An agent login comes back typed User, as GitHub types the field.
        (trust_stubs.IMPOSTOR, "vergil-user", "wrong-type"),
        # A bot or App email links to no user account.
        (None, "an author with no GitHub user account", "deleted"),
    ],
)
def test_relay_payload_untrusted_commit_author_raises(
    clone: Path, commits: _Commits, user: dict[str, str] | None, who: str, cause: str
) -> None:
    commits.user = user
    transport = GitHubTransport(_BRANCH)
    transport.write(_state())
    with pytest.raises(trust.UntrustedContentError) as raised:
        transport.read()
    assert str(raised.value) == (
        "stopped before any write: the commit's author is untrusted: "
        f"{_REPO} commit {who} at {trust_stubs.CREATED} on the relay ref {transport.ref} "
        f"({cause}). The human decides next."
    )


def test_relay_payload_unreadable_author_raises(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transport = GitHubTransport(_BRANCH)
    transport.write(_state())
    monkeypatch.setattr(github, "graphql", lambda _query, **_vars: {"repository": {"object": None}})
    with pytest.raises(trust.TrustReadError, match="the commit is missing or not an object"):
        transport.read()


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


def test_write_keeps_stdout_clean_routing_push_chatter_to_stderr(
    clone: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Quiet-by-design (#2793): ``write`` must not leak git push chatter to
    stdout — outside a progress pipeline ``git.run`` prints its streamed output,
    and a caller with a JSON-on-stdout contract (``report-ready``) would be
    corrupted. The transport redirects the chatter to stderr itself, so no
    caller-side ``redirect_stdout`` workaround is needed."""
    GitHubTransport(_BRANCH).write(_state())
    captured = capsys.readouterr()
    assert captured.out == ""


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
