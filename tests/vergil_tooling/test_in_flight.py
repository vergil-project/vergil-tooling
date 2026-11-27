"""Tests for vergil_tooling.lib.in_flight (the in-flight marks of ad-hoc tasks)."""

from __future__ import annotations

import base64
import json
import subprocess
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from vergil_tooling.lib import (
    github,
    in_flight,
    live_branches,
    pending_release,
    queue_view,
    trust,
)
from vergil_tooling.lib.epics import ChildState, IssueRef
from vergil_tooling.lib.in_flight import InFlightError, RepoSignals
from vergil_tooling.lib.trusted_authors import (
    OrgConfig,
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MOD = "vergil_tooling.lib.in_flight"
_REPO = "org/tool"
_URL = "https://github.com/org/tool.git"


def _payload(issue: str, status: str = "ready", issues: list[str] | None = None) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 2,
        "issue": issue,
        "branch": "feature/x",
        "base": "develop",
        "status": status,
        "created_at": "2026-11-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
        "git": {},
        "pr_metadata": None,
        "submitted": None,
    }
    if issues is not None:
        data["issues"] = issues
    return data


_TRUSTED = TrustedAuthors(
    human=("human",), agents=("agent",), orgs={"org": OrgConfig("org-automation")}
)
_CREATED = "2026-11-01T09:30:00Z"
_TYPES = {"human": "User", "agent": "Bot", "org-automation": "Bot", "mallory": "User"}


def _actor(who: str | None) -> dict[str, str] | None:
    return None if who is None else {"__typename": _TYPES[who], "login": who}


def _issue(number: int, repo: str = _REPO) -> dict[str, Any]:
    return {"number": number, "repository": {"nameWithOwner": repo}}


def _pr(
    number: int,
    head: str = "fix-thing",
    *,
    closes: tuple[int | tuple[str, int], ...] = (),
    body: str | None = None,
    author: str | None = "human",
    edits: tuple[str, ...] = (),
    fork: bool = False,
    linked: tuple[tuple[str, int], ...] = (),
) -> dict[str, Any]:
    """An open PR node as the in-flight query reads it; *closes* are in the body by default.

    *linked* holds each hand link as ``(linker, issue)``: a connected event and
    a closing reference.
    """
    refs = [_issue(c) if isinstance(c, int) else _issue(c[1], c[0]) for c in closes]
    refs += [_issue(issue) for _, issue in linked]
    if body is None:
        body = "\n".join(
            f"Closes #{c}" if isinstance(c, int) else f"Closes {c[0]}#{c[1]}" for c in closes
        )
    return {
        "number": number,
        "headRefName": head,
        "isCrossRepository": fork,
        "headRepositoryOwner": {"login": "someone" if fork else "org"},
        "headRepository": {"name": "tool"},
        "repository": {"nameWithOwner": _REPO},
        "author": _actor(author),
        "editor": _actor(edits[-1]) if edits else None,
        "body": body,
        "createdAt": _CREATED,
        "lastEditedAt": _CREATED if edits else None,
        "userContentEdits": {
            "pageInfo": {"hasNextPage": False, "endCursor": None},
            "nodes": [{"editedAt": _CREATED, "editor": _actor(e)} for e in edits],
        },
        "closingIssuesReferences": {"totalCount": len(refs), "nodes": refs},
        "timelineItems": {
            "filteredCount": len(linked),
            "nodes": [
                {"actor": _actor(who), "createdAt": _CREATED, "subject": _issue(n), "source": {}}
                for who, n in linked
            ],
        },
    }


def _page(nodes: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {
        "repository": {
            "pullRequests": {
                "pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor},
                "nodes": nodes,
            }
        }
    }


@pytest.fixture(autouse=True)
def _authors() -> Iterator[None]:
    """The trusted authors, and a repo with no open PR unless a test serves some."""
    with (
        patch(f"{_MOD}.load_trusted_authors", return_value=_TRUSTED),
        patch(f"{_MOD}.github.graphql", return_value=_page([])),
    ):
        yield


class _FakeRemote:
    """``github.graphql``, ``github.read_json`` and ``git.read_output`` stand-ins for a remote.

    *prs* are the open PR nodes, served one page of :data:`_PER_PAGE` at a
    time. *relays* maps a relay ref's branch to its payload (a dict, or raw
    bytes for a malformed blob); each gets the commit SHA ``c-<branch>``.
    """

    _PER_PAGE = 2

    def __init__(
        self,
        *,
        prs: list[dict[str, Any]] | None = None,
        branches: tuple[str, ...] = (),
        relays: dict[str, Any] | None = None,
    ) -> None:
        self.prs = prs or []
        self.branches = branches
        self.relays = relays or {}
        self.gh_calls: list[tuple[str, ...]] = []
        self.git_calls: list[tuple[str, ...]] = []
        self.graphql_calls: list[dict[str, object]] = []

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        assert query == in_flight._OPEN_PRS_QUERY
        self.graphql_calls.append(variables)
        start = int(str(variables.get("after", "0")))
        end = start + self._PER_PAGE
        more = end < len(self.prs)
        return _page(self.prs[start:end], str(end) if more else None)

    def gh(self, *args: str) -> Any:
        self.gh_calls.append(args)
        assert args[0] == "api"
        path = args[1].removeprefix(f"repos/{_REPO}/git/")
        kind, _, key = path.partition("/")
        if kind == "commits":
            return {"sha": key, "tree": {"sha": f"t-{key}"}}
        if kind == "trees":
            return {"tree": [{"path": "pr-workflow.json", "type": "blob", "sha": f"b-{key}"}]}
        assert kind == "blobs"
        branch = key.removeprefix("b-t-c-")
        payload = self.relays[branch]
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return {"encoding": "base64", "content": base64.b64encode(raw).decode()}

    def git(self, *args: str) -> str:
        self.git_calls.append(args)
        assert args[2] == "ls-remote"
        lines = [f"h-{b}\trefs/heads/{b}" for b in self.branches]
        lines += [f"c-{b}\trefs/vergil/pr-workflow/{b}" for b in self.relays]
        # A ref outside both namespaces is never a mark.
        lines.append("x-1\trefs/tags/feature/7-x")
        return "\n".join(lines)


def _signals(remote: _FakeRemote, clone: Path, *, local: tuple[str, ...] = ()) -> RepoSignals:
    with (
        patch(f"{_MOD}.github.graphql", side_effect=remote.graphql),
        patch(f"{_MOD}.github.read_json", side_effect=remote.gh),
        patch(f"{_MOD}.git.read_output", side_effect=remote.git),
        patch(f"{_MOD}.git.local_branches", return_value=set(local)),
    ):
        return in_flight.repo_signals(_REPO, clone)


def _marks(issue: int, remote: _FakeRemote, clone: Path, **kw: Any) -> tuple[str, ...]:
    return in_flight.marks_for(issue, _signals(remote, clone, **kw))


# -- the marks ---------------------------------------------------------------------


def test_in_flight_open_pr_closing_the_issue(tmp_path: Path) -> None:
    remote = _FakeRemote(prs=[_pr(90, closes=(7,))])
    assert _marks(7, remote, tmp_path) == ("open-pr",)
    assert _marks(8, remote, tmp_path) == ()
    assert remote.graphql_calls[0] == {"owner": "org", "name": "tool"}


def test_in_flight_open_pr_query_reads_what_the_judgement_needs() -> None:
    query = in_flight._OPEN_PRS_QUERY
    assert "pullRequests(states: [OPEN]" in query
    assert trust.SAME_REPO_FIELDS in query
    assert pending_release.CLOSING_FIELDS in query
    assert "headRefName" in query


def test_in_flight_open_prs_read_every_page(tmp_path: Path) -> None:
    prs = [_pr(90 + n, f"feature/{n}-x") for n in range(5)]
    remote = _FakeRemote(prs=prs)
    signals = _signals(remote, tmp_path)
    assert signals.open_pr_issues == frozenset(range(5))
    assert [call.get("after") for call in remote.graphql_calls] == [None, "2", "4"]


def test_in_flight_open_pr_closing_another_repos_issue_marks_nothing(tmp_path: Path) -> None:
    prs = [_pr(90, closes=(("o/n", 7),))]
    assert _marks(7, _FakeRemote(prs=prs), tmp_path) == ()


def test_in_flight_open_pr_on_feature_branch(tmp_path: Path) -> None:
    assert _marks(7, _FakeRemote(prs=[_pr(90, "feature/7-x")]), tmp_path) == ("open-pr",)


def test_in_flight_open_pr_by_trusted_agent_counts(tmp_path: Path) -> None:
    prs = [_pr(90, "feature/7-x", author="agent"), _pr(91, closes=(8,), author="org-automation")]
    signals = _signals(_FakeRemote(prs=prs), tmp_path)
    assert signals.open_pr_issues == frozenset({7, 8})


@pytest.mark.parametrize(
    ("pr", "cause"),
    [
        (_pr(90, "feature/7-x", closes=(7,), fork=True, author="human"), "cross-repository"),
        (_pr(90, "feature/7-x", closes=(7,), author="mallory"), "untrusted"),
        (_pr(90, "feature/7-x", closes=(7,), author=None), "deleted"),
    ],
)
def test_in_flight_fork_or_outsider_pr_marks_nothing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pr: dict[str, Any],
    cause: str,
) -> None:
    assert _marks(7, _FakeRemote(prs=[pr]), tmp_path) == ()
    err = capsys.readouterr().err
    assert f"skipped untrusted content: {_REPO}#90 pr" in err
    assert f"({cause})" in err


def test_in_flight_skipped_pr_is_counted_only_under_actions(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    prs = [_pr(90, "feature/7-x", author="mallory"), _pr(91, "feature/8-x", fork=True)]
    assert _marks(7, _FakeRemote(prs=prs), tmp_path) == ()
    err = capsys.readouterr().err
    assert "skipped 2 untrusted item(s) (cross-repository: 1, untrusted: 1)" in err
    assert "mallory" not in err


def test_in_flight_untrusted_body_keeps_only_the_head_mark(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prs = [_pr(90, "feature/7-x", closes=(8,), edits=("mallory",))]
    signals = _signals(_FakeRemote(prs=prs), tmp_path)
    # The trusted author's head still marks 7; the closing line of a body an
    # outsider edited marks nothing.
    assert signals.open_pr_issues == frozenset({7})
    assert f"{_REPO}#90 edit mallory" in capsys.readouterr().err


def test_in_flight_hand_link_counts_only_by_a_trusted_linker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    prs = [_pr(90, linked=(("human", 7), ("mallory", 8)))]
    signals = _signals(_FakeRemote(prs=prs), tmp_path)
    assert signals.open_pr_issues == frozenset({7})
    assert "link to #8" in capsys.readouterr().err


def test_in_flight_report_ready_relay_ref(tmp_path: Path) -> None:
    remote = _FakeRemote(relays={"feature/7-x": _payload("7")})
    # The relay's branch is not on the remote: only the ready record marks it.
    assert _marks(7, remote, tmp_path) == ("report-ready",)
    # Read by SHA through the Git Data API: commit, tree, blob.
    assert [c[1] for c in remote.gh_calls if c[0] == "api"] == [
        f"repos/{_REPO}/git/commits/c-feature/7-x",
        f"repos/{_REPO}/git/trees/t-c-feature/7-x",
        f"repos/{_REPO}/git/blobs/b-t-c-feature/7-x",
    ]
    assert remote.git_calls == [
        (
            "-C",
            str(tmp_path),
            "ls-remote",
            _URL,
            "refs/heads/feature/*",
            "refs/vergil/pr-workflow/*",
        )
    ]


def test_in_flight_unfrozen_relay_is_not_report_ready(tmp_path: Path) -> None:
    remote = _FakeRemote(relays={"feature/7-x": _payload("7", status="implementing")})
    assert _marks(7, remote, tmp_path) == ()


def test_in_flight_report_ready_batch_member(tmp_path: Path) -> None:
    remote = _FakeRemote(
        relays={
            "feature/7-batch-x": _payload("7", issues=["7", "org/tool#9"]),
            "feature/11-y": _payload("11"),
        }
    )
    signals = _signals(remote, tmp_path)
    # 9 has no branch and no local batch marker; the batch payload marks it.
    assert in_flight.marks_for(9, signals) == ("report-ready",)
    assert in_flight.marks_for(7, signals) == ("report-ready",)
    # A single-issue payload marks its one issue.
    assert in_flight.marks_for(11, signals) == ("report-ready",)
    assert signals.ready_relay_issues == frozenset({7, 9, 11})


def test_in_flight_relay_issue_forms(tmp_path: Path) -> None:
    remote = _FakeRemote(
        relays={
            "feature/42-a": _payload("42"),
            "feature/43-b": _payload("org/tool#43"),
            "feature/44-c": _payload("other/repo#44"),
        }
    )
    signals = _signals(remote, tmp_path)
    assert signals.ready_relay_issues == frozenset({42, 43})
    bad = _FakeRemote(relays={"feature/45-d": _payload("not-a-ref")})
    with pytest.raises(InFlightError, match=r"org/tool: .*feature/45-d.*'not-a-ref'"):
        _signals(bad, tmp_path)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"not json", "malformed relay payload"),
        (b"[1, 2]", "not a JSON object"),
        ({**_payload("7"), "schema_version": 1}, "unsupported schema_version"),
        ({**_payload("7"), "status": "weird"}, "invalid status"),
    ],
)
def test_in_flight_malformed_relay_payload_raises(tmp_path: Path, payload: Any, match: str) -> None:
    remote = _FakeRemote(relays={"feature/7-x": payload})
    with pytest.raises(InFlightError, match=match):
        _signals(remote, tmp_path)


def test_in_flight_relay_without_its_file_raises(tmp_path: Path) -> None:
    remote = _FakeRemote(relays={"feature/7-x": _payload("7")})

    def gh(*args: str) -> Any:
        if args[0] == "api" and "/trees/" in args[1]:
            return {"tree": []}
        return remote.gh(*args)

    with (
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(f"{_MOD}.git.read_output", side_effect=remote.git),
        pytest.raises(InFlightError, match="relay ref of feature/7-x"),
    ):
        in_flight.repo_signals(_REPO, tmp_path)


def test_in_flight_remote_and_local_branch(tmp_path: Path) -> None:
    remote = _FakeRemote(branches=("feature/7-x",))
    assert _marks(7, remote, tmp_path) == ("branch",)
    assert _marks(7, _FakeRemote(), tmp_path, local=("feature/7-x",)) == ("worktree",)
    assert _marks(7, remote, tmp_path, local=("feature/7-x", "develop")) == (
        "branch",
        "worktree",
    )


def _state_dir(clone: Path, issue: int) -> Path:
    path = clone / "build" / ".vergil-state" / "issue-implement" / str(issue)
    path.mkdir(parents=True)
    return path


def test_in_flight_merge_review_directory(tmp_path: Path) -> None:
    (_state_dir(tmp_path, 7) / "merge-review").mkdir()
    assert _marks(7, _FakeRemote(), tmp_path) == ("merge-review",)
    assert _marks(8, _FakeRemote(), tmp_path) == ()


def test_in_flight_batch_marker_counts(tmp_path: Path) -> None:
    # Review Focus 2: a batch member with no feature/<N>- branch of its own.
    (_state_dir(tmp_path, 9) / "batch").write_text("{}")
    marks = _marks(9, _FakeRemote(branches=("feature/7-batch-x",)), tmp_path)
    assert marks == ("batch",)
    assert in_flight.disposition(marks) == "resume"


def test_in_flight_prefix_does_not_overmatch(tmp_path: Path) -> None:
    prs = [_pr(90, "feature/12-x")]
    remote = _FakeRemote(prs=prs, branches=("feature/12-x", "feature/1x"))
    assert _marks(1, remote, tmp_path, local=("feature/12-x", "feature/1")) == ()
    assert _marks(12, remote, tmp_path) == ("open-pr", "branch")


def test_in_flight_marks_in_order(tmp_path: Path) -> None:
    (_state_dir(tmp_path, 7) / "merge-review").mkdir()
    (tmp_path / "build" / ".vergil-state" / "issue-implement" / "7" / "batch").write_text("{}")
    remote = _FakeRemote(
        prs=[_pr(90, "feature/7-x")],
        branches=("feature/7-x",),
        relays={"feature/7-x": _payload("7")},
    )
    assert _marks(7, remote, tmp_path, local=("feature/7-x",)) == in_flight.MARKS


# -- disposition ---------------------------------------------------------------------


def test_disposition_ranks_handed_off_over_local() -> None:
    assert in_flight.disposition(("report-ready", "branch", "worktree")) == "handed-off"
    assert in_flight.disposition(("open-pr", "worktree")) == "handed-off"
    assert in_flight.disposition(("worktree",)) == "resume"
    assert in_flight.disposition(("merge-review",)) == "resume"
    assert in_flight.disposition(("batch", "branch")) == "resume"
    assert in_flight.disposition(("branch",)) == "handed-off"
    assert in_flight.disposition(()) is None


# -- read failures --------------------------------------------------------------------


def test_in_flight_read_failure_raises(tmp_path: Path) -> None:
    failure = github.GitHubAPIError(1, ["gh", "api", "graphql"], "", "HTTP 502")
    with (
        patch(f"{_MOD}.github.graphql", side_effect=failure),
        pytest.raises(InFlightError, match=r"org/tool: cannot read its open PRs: .*502"),
    ):
        in_flight.repo_signals(_REPO, tmp_path)
    remote = _FakeRemote()
    ls_failure = subprocess.CalledProcessError(128, ["git", "ls-remote"], "", "denied")
    with (
        patch(f"{_MOD}.github.read_json", side_effect=remote.gh),
        patch(f"{_MOD}.git.read_output", side_effect=ls_failure),
        pytest.raises(InFlightError, match="org/tool: cannot list its remote refs"),
    ):
        in_flight.repo_signals(_REPO, tmp_path)


def test_in_flight_relay_read_failure_raises(tmp_path: Path) -> None:
    remote = _FakeRemote(relays={"feature/7-x": _payload("7")})
    failure = github.GitHubAPIError(1, ["gh", "api"], "", "HTTP 404")

    def gh(*args: str) -> Any:
        if args[0] == "api":
            raise failure
        return remote.gh(*args)

    with (
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(f"{_MOD}.git.read_output", side_effect=remote.git),
        pytest.raises(InFlightError, match=r"cannot read the relay ref of feature/7-x: .*404"),
    ):
        in_flight.repo_signals(_REPO, tmp_path)


def test_in_flight_local_branch_failure_raises(tmp_path: Path) -> None:
    remote = _FakeRemote()
    failure = subprocess.CalledProcessError(128, ["git", "branch"], "", "corrupt")
    with (
        patch(f"{_MOD}.github.read_json", side_effect=remote.gh),
        patch(f"{_MOD}.git.read_output", side_effect=remote.git),
        patch(f"{_MOD}.git.local_branches", side_effect=failure),
        pytest.raises(InFlightError, match="cannot read the local branches"),
    ):
        in_flight.repo_signals(_REPO, tmp_path)


@pytest.mark.parametrize(
    ("page", "match"),
    [
        ({"repository": None}, "cannot read its open PRs"),
        (_page([{"number": 90}]), "cannot read its open PRs: 'author'"),
        (_page(["row"]), "cannot read its open PRs"),
        (
            {"repository": {"pullRequests": {"pageInfo": {}, "nodes": None}}},
            "unexpected pullRequests nodes: None",
        ),
        (
            {"repository": {"pullRequests": {"pageInfo": {"hasNextPage": True}, "nodes": []}}},
            "cannot read its open PRs",
        ),
        (_page([], cursor=""), "next page but no cursor"),
        (_page([{**_pr(90), "isCrossRepository": None}]), "no isCrossRepository flag"),
        (_page([{**_pr(90), "author": {"login": "human"}}]), "cannot read its open PRs"),
    ],
)
def test_in_flight_malformed_pr_page_raises(tmp_path: Path, page: Any, match: str) -> None:
    with (
        patch(f"{_MOD}.github.graphql", return_value=page),
        pytest.raises(InFlightError, match=match),
    ):
        in_flight.repo_signals(_REPO, tmp_path)


def test_in_flight_truncated_closing_refs_raise(tmp_path: Path) -> None:
    pr = _pr(90, closes=(7,))
    pr["closingIssuesReferences"]["totalCount"] = 30
    with pytest.raises(InFlightError, match="closingIssuesReferences of #90 truncated"):
        _signals(_FakeRemote(prs=[pr]), tmp_path)


def test_in_flight_unconfigured_authors_raise(tmp_path: Path) -> None:
    with (
        patch(
            f"{_MOD}.load_trusted_authors",
            side_effect=TrustedAuthorsNotConfiguredError("no trusted authors"),
        ),
        pytest.raises(InFlightError, match="cannot read its open PRs: no trusted authors"),
    ):
        in_flight.repo_signals(_REPO, tmp_path)


def test_in_flight_missing_token_propagates(tmp_path: Path) -> None:
    failure = github.MissingGitHubTokenError(1, ["gh"], stderr="GH_TOKEN")
    with (
        patch(f"{_MOD}.github.graphql", side_effect=failure),
        pytest.raises(github.MissingGitHubTokenError),
    ):
        in_flight.repo_signals(_REPO, tmp_path)


# -- find_clones -----------------------------------------------------------------------


@pytest.fixture
def roots(tmp_path: Path) -> Iterator[Path]:
    """A projects root: ``org/tool``, ``org/plugin``, ``org/other`` and ``org/gitlab``."""
    for name in ("tool", "other", "plugin", "gitlab"):
        (tmp_path / "org" / name / ".git").mkdir(parents=True)
    origins = {
        "tool": "https://github.com/org/tool.git",
        "plugin": "git@github.com:Org/Plugin.git",
        "gitlab": "https://gitlab.com/org/tool.git",
    }

    def names(repo: Path) -> set[str]:
        return {"origin"} if repo.name in origins else set()

    with (
        patch(f"{_MOD}.git.remote_names", side_effect=names),
        patch(f"{_MOD}.git.remote_url", side_effect=lambda _r, repo: origins[repo.name]),
    ):
        yield tmp_path


def test_find_clones_maps_repos_by_origin(roots: Path) -> None:
    clones = in_flight.find_clones(["org/tool", "org/plugin"], [roots])
    assert clones == {
        "org/tool": (roots / "org" / "tool").resolve(),
        "org/plugin": (roots / "org" / "plugin").resolve(),
    }
    assert in_flight.find_clones([], [roots]) == {}
    # The checkout whose origin is on another host is not org/tool's clone.
    assert in_flight.find_clones(["org/tool"], [roots]) == {
        "org/tool": (roots / "org" / "tool").resolve()
    }


def test_in_flight_repo_without_clone_is_fatal(roots: Path) -> None:
    # Review Focus 3: the error names the repo and the roots searched.
    with pytest.raises(InFlightError, match=rf"org/missing .*no main clone .*\({roots}\)"):
        in_flight.find_clones(["org/tool", "org/missing"], [roots])


def test_find_clones_refuses_two_clones(roots: Path) -> None:
    second = roots / "elsewhere"
    (second / ".git").mkdir(parents=True)
    with (
        patch(f"{_MOD}.git.remote_names", return_value={"origin"}),
        patch(f"{_MOD}.git.remote_url", return_value=_URL),
        pytest.raises(InFlightError, match="more than one clone"),
    ):
        in_flight.find_clones(["org/tool"], [roots])


def test_find_clones_remote_read_failure_raises(roots: Path) -> None:
    failure = subprocess.CalledProcessError(128, ["git", "remote"], "", "broken")
    with (
        patch(f"{_MOD}.git.remote_names", side_effect=failure),
        pytest.raises(InFlightError, match="cannot read the origin remote"),
    ):
        in_flight.find_clones(["org/tool"], [roots])


def test_find_clones_missing_root_raises(tmp_path: Path) -> None:
    with pytest.raises(live_branches.RepoSetError, match="does not exist"):
        in_flight.find_clones(["org/tool"], [tmp_path / "nope"])


# -- adhoc_items with in-flight marks ---------------------------------------------------

_QV = "vergil_tooling.lib.queue_view"


def test_adhoc_items_in_flight_outranks_blocked(tmp_path: Path) -> None:
    children = [
        ChildState(IssueRef("org", "tool", 3), "OPEN", "held"),
        ChildState(IssueRef("org", "tool", 5), "OPEN", "free"),
    ]
    views = {
        3: {"title": "held", "createdAt": "2026-09-01T00:00:00Z", "labels": []},
        5: {"title": "free", "createdAt": "2026-10-01T00:00:00Z", "labels": []},
    }
    listing = [{"number": 100, "title": "Epic (ad hoc): tool"}]

    def gh(*args: str) -> Any:
        return listing if args[:2] == ("issue", "list") else views[int(args[2])]

    signals = RepoSignals(
        _REPO, tmp_path, frozenset({3}), frozenset({"feature/3-x"}), frozenset(), frozenset()
    )
    with (
        patch(f"{_QV}.github.read_json", side_effect=gh),
        patch(f"{_QV}.epics.child_states_with_blockers", return_value=children),
        patch(f"{_QV}.epics.blockers_of", return_value=[IssueRef("org", "tool", 40)]),
        patch(f"{_QV}.epics.native_blockers_of", return_value=()),
        patch(f"{_QV}.epics.issue_state", return_value="OPEN"),
        patch(f"{_QV}.in_flight.find_clones", return_value={_REPO: tmp_path}) as find,
        patch(f"{_QV}.in_flight.repo_signals", return_value=signals) as read,
    ):
        stops = trust.BatchStops()
        held, free = queue_view.adhoc_items("org/.github", stops=stops, roots=[tmp_path])
    find.assert_called_once_with([_REPO], [tmp_path])
    read.assert_called_once_with(_REPO, tmp_path)
    assert held.state == "in-flight"
    assert held.marks == ("open-pr", "branch")
    assert held.disposition == "handed-off"
    assert held.blocked_by == (IssueRef("org", "tool", 40),)
    assert free.state == "blocked"
    assert free.marks == ()
    assert free.disposition is None


def test_adhoc_items_default_roots_and_no_tasks_reads_nothing() -> None:
    listing = [{"number": 100, "title": "Epic (ad hoc): tool"}]
    with (
        patch(f"{_QV}.github.read_json", return_value=listing),
        patch(f"{_QV}.epics.child_states_with_blockers", return_value=[]),
        patch(f"{_QV}.live_branches.default_repo_roots") as defaults,
        patch(f"{_QV}.in_flight.find_clones") as find,
    ):
        assert queue_view.adhoc_items("org/.github", stops=trust.BatchStops()) == []
    defaults.assert_not_called()
    find.assert_not_called()


def test_adhoc_items_no_roots_on_the_platform_raises(tmp_path: Path) -> None:
    listing = [{"number": 100, "title": "Epic (ad hoc): tool"}]
    view = {"title": "t", "createdAt": "2026-09-01T00:00:00Z", "labels": []}
    with (
        patch(
            f"{_QV}.github.read_json", side_effect=lambda *a: listing if a[1] == "list" else view
        ),
        patch(
            f"{_QV}.epics.child_states_with_blockers",
            return_value=[ChildState(IssueRef("org", "tool", 3), "OPEN", "t")],
        ),
        patch(f"{_QV}.epics.blockers_of", return_value=[]),
        patch(f"{_QV}.epics.native_blockers_of", return_value=()),
        patch(
            f"{_QV}.live_branches.default_repo_roots",
            side_effect=live_branches.RepoSetError("no default repo set"),
        ),
        pytest.raises(live_branches.RepoSetError, match="no default repo set"),
    ):
        queue_view.adhoc_items("org/.github", stops=trust.BatchStops())


def test_render_adhoc_text_in_flight() -> None:
    from datetime import UTC, datetime

    item = queue_view.AdhocItem(
        IssueRef("org", "tool", 3),
        "held",
        "2026-11-01T00:00:00Z",
        "task",
        (IssueRef("org", "tool", 40),),
        "in-flight",
        ("open-pr", "worktree"),
        "handed-off",
    )
    now = datetime(2026, 11, 3, tzinfo=UTC)
    assert queue_view.render_adhoc_text([item], now=now) == (
        "org/tool#3  2d  in-flight  task  held  — handed-off: open-pr, worktree"
        "  — blocked by org/tool#40"
    )
    assert queue_view.adhoc_to_json([item])[0]["marks"] == ["open-pr", "worktree"]
    assert queue_view.adhoc_to_json([item])[0]["disposition"] == "handed-off"


@pytest.mark.parametrize(
    ("blob", "match"),
    [
        (["not", "an", "object"], "unexpected response reading the relay ref of feature/7-x"),
        ({"encoding": "utf-8", "content": "{}"}, "unexpected blob encoding 'utf-8'"),
    ],
)
def test_in_flight_unexpected_blob_raises(tmp_path: Path, blob: Any, match: str) -> None:
    remote = _FakeRemote(relays={"feature/7-x": _payload("7")})

    def gh(*args: str) -> Any:
        if args[0] == "api" and "/blobs/" in args[1]:
            return blob
        return remote.gh(*args)

    with (
        patch(f"{_MOD}.github.read_json", side_effect=gh),
        patch(f"{_MOD}.git.read_output", side_effect=remote.git),
        pytest.raises(InFlightError, match=match),
    ):
        in_flight.repo_signals(_REPO, tmp_path)


def test_in_flight_unreadable_state_path_raises(tmp_path: Path) -> None:
    signals = RepoSignals(_REPO, tmp_path, frozenset(), frozenset(), frozenset(), frozenset())
    with (
        patch("pathlib.Path.exists", side_effect=PermissionError("denied")),
        pytest.raises(InFlightError, match=r"org/tool: cannot check .*merge-review: denied"),
    ):
        in_flight.marks_for(7, signals)
