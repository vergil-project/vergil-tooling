"""Superseding a release whose tracking issue fails the identity rule (#4878).

The breached issue's state block is never read: the release is rebuilt from
GitHub and git facts (``lib/release/rebuild.py``).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.lib.pending_release import PendingReleaseError
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.guard import TrackingIssueBreach
from vergil_tooling.lib.release.rebuild import Rebuilt, rebuild_breached
from vergil_tooling.lib.release.state import IssueEntry
from vergil_tooling.lib.release.supersede import Superseded, load_superseded

if TYPE_CHECKING:
    from collections.abc import Iterator

_REBUILD = "vergil_tooling.lib.release.rebuild"
_SUPERSEDE = "vergil_tooling.lib.release.supersede"
_REPO = "owner/repo"
_ROOT = Path("/tmp/repo")  # noqa: S108
_OLD = "2.1.5"
_PRIOR = "2.1.4"
_MERGE = "m" * 40
_PARENT = "p" * 40
_HEAD = "h" * 40
_PRIOR_MERGE = "n" * 40
_PRIOR_HEAD = "i" * 40
_BREACH = TrackingIssueBreach("mallory", "edited the body", number=40)
_OWN = (IssueEntry(f"{_REPO}#11", (8,)),)
_PRIOR_ISSUES = (IssueEntry(f"{_REPO}#10", (7,)), IssueEntry(f"{_REPO}#11", (6,)))


def _pr(merge: str, head: str, number: int) -> dict[str, Any]:
    return {
        "url": f"https://github.com/{_REPO}/pull/{number}",
        "mergeCommit": {"oid": merge},
        "headRefOid": head,
    }


def _closing_comment(login: str, version: str = _OLD, number: int = 40) -> dict[str, Any]:
    return {
        "author": {"login": login},
        "body": f"Superseded by release {version}: https://github.com/{_REPO}/issues/{number}\n\nmore",
    }


@dataclass
class _Facts:
    """What GitHub and git answer about the releases, by default a tagged 2.1.5."""

    merged: dict[str, list[dict[str, Any]]] = field(
        default_factory=lambda: {
            f"release/{_OLD}": [_pr(_MERGE, _HEAD, 50)],
            f"release/post-{_OLD}": [_pr("b" * 40, "c" * 40, 51)],
        }
    )
    tags: dict[str, str] = field(default_factory=lambda: {_OLD: _MERGE})
    runs: list[dict[str, Any]] = field(default_factory=list)
    jobs: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    closed: list[dict[str, Any]] = field(default_factory=list)
    parents: dict[str, str] = field(default_factory=dict)
    # An answer, or an exception to raise, in place of a gh command's, by its
    # first two words.
    answers: dict[tuple[str, ...], object] = field(default_factory=dict)

    def read_json(self, *args: str) -> object:
        if args[:2] in self.answers:
            answer = self.answers[args[:2]]
            if isinstance(answer, Exception):
                raise answer
            return answer
        if args[:2] == ("pr", "list"):
            return self.merged.get(args[args.index("--head") + 1], [])
        if args[:2] == ("run", "list"):
            return self.runs
        if args[:2] == ("run", "view"):
            return {"jobs": self.jobs[args[2]]}
        if args[:2] == ("issue", "list"):
            return self.closed
        raise AssertionError(args)

    def git(self, *args: str) -> str:
        if args[0] == "ls-remote":
            version = args[-1].removeprefix("refs/tags/v")
            return f"{'t' * 40}\t{args[-1]}" if version in self.tags else ""
        if args[0] == "rev-parse" and args[1].endswith("^{commit}"):
            return self.tags[args[1].removeprefix("v").removesuffix("^{commit}")]
        if args[0] == "rev-parse" and args[1].endswith("^1"):
            return {_MERGE: _PARENT, _PRIOR_MERGE: "q" * 40}[args[1].removesuffix("^1")]
        if args[0] == "rev-list":
            return f"{args[-1]} {self.parents.get(args[-1], 'z' * 40)}"
        raise AssertionError(args)


@pytest.fixture
def facts() -> Iterator[tuple[_Facts, dict[str, MagicMock]]]:
    found = _Facts()
    with (
        patch(_REBUILD + ".github.read_json", side_effect=found.read_json) as m_json,
        patch(_REBUILD + ".git.read_output", side_effect=found.git),
        patch(_REBUILD + ".git.run") as m_run,
        patch(_REBUILD + ".gather_issue_list", return_value=_OWN) as m_issues,
        patch(_REBUILD + ".read_state") as m_read,
        patch(_REBUILD + "._human_logins", return_value=("human",)),
    ):
        yield found, {"json": m_json, "run": m_run, "issues": m_issues, "read": m_read}


def _rebuild() -> Rebuilt:
    return rebuild_breached(_REPO, _ROOT, _OLD, 40, _BREACH)


def _untagged(found: _Facts, *, release: str = "failure", status: str = "completed") -> None:
    del found.tags[_OLD]
    del found.merged[f"release/post-{_OLD}"]
    found.runs = [{"databaseId": 111, "status": status}]
    found.jobs = {"111": [{"name": "release / release", "conclusion": release}]}


def test_tagged_release_is_rebuilt_from_its_facts(
    facts: tuple[_Facts, dict[str, MagicMock]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, mocks = facts
    assert _rebuild() == Rebuilt(issues=_OWN, main_commit=None, tag=f"v{_OLD}")
    mocks["issues"].assert_called_once_with(base=_PARENT, head=_HEAD)
    mocks["read"].assert_not_called()
    err = capsys.readouterr().err
    assert "#40 of release 2.1.5 fails the identity rule (mallory edited the body)" in err
    assert "Its state block is not read" in err


def test_tag_on_a_freeze_commit_on_the_release_merge_is_accepted(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, _ = facts
    found.tags[_OLD] = "f" * 40
    found.parents["f" * 40] = _MERGE
    assert _rebuild().tag == f"v{_OLD}"


def test_tag_off_the_release_merge_refuses(facts: tuple[_Facts, dict[str, MagicMock]]) -> None:
    found, _ = facts
    found.tags[_OLD] = "f" * 40
    with pytest.raises(ReleaseError, match="is neither the merge commit"):
        _rebuild()


def test_tagged_release_without_a_merged_back_merge_refuses(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, _ = facts
    del found.merged[f"release/post-{_OLD}"]
    with pytest.raises(ReleaseError, match="back-merge PR .* is not merged"):
        _rebuild()


def test_unmerged_release_refuses_with_the_manual_recovery(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, mocks = facts
    found.merged.clear()
    with pytest.raises(ReleaseError, match="nothing of it reached main") as info:
        _rebuild()
    assert "Close its release PR and tracking issue #40" in str(info.value)
    mocks["issues"].assert_not_called()


def test_two_merged_release_prs_refuse(facts: tuple[_Facts, dict[str, MagicMock]]) -> None:
    found, _ = facts
    found.merged[f"release/{_OLD}"].append(_pr("x" * 40, "y" * 40, 49))
    with pytest.raises(ReleaseError, match="2 PRs of release/2.1.5 into main are merged"):
        _rebuild()


def test_untagged_release_whose_release_job_failed_takes_the_merge_commit(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, _ = facts
    _untagged(found)
    assert _rebuild() == Rebuilt(issues=_OWN, main_commit=_MERGE, tag=None)


def test_untagged_release_whose_release_job_succeeded_refuses(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, _ = facts
    _untagged(found, release="success")
    with pytest.raises(ReleaseError, match="release job 'release / release' succeeded"):
        _rebuild()


def test_untagged_release_with_a_cd_run_in_progress_refuses(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, _ = facts
    _untagged(found, status="in_progress")
    with pytest.raises(ReleaseError, match="is in_progress, not completed"):
        _rebuild()


def test_untagged_release_with_no_cd_run_refuses(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, _ = facts
    _untagged(found)
    found.runs = []
    with pytest.raises(ReleaseError, match="no main CD run exists"):
        _rebuild()


def test_a_failed_lookup_refuses(facts: tuple[_Facts, dict[str, MagicMock]]) -> None:
    _, mocks = facts
    mocks["json"].side_effect = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
    with pytest.raises(ReleaseError, match="could not be read") as info:
        _rebuild()
    assert "HTTP 502" in (info.value.detail or "")


def test_an_issue_list_that_cannot_be_rebuilt_refuses(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    _, mocks = facts
    mocks["issues"].side_effect = PendingReleaseError("compare failed")
    with pytest.raises(ReleaseError, match="issue list could not be rebuilt: compare failed"):
        _rebuild()


_FAILED = subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")


@pytest.mark.parametrize(
    ("untagged", "command", "answer", "match"),
    [
        (False, ("pr", "list"), {"not": "a list"}, "is not a list"),
        (False, ("pr", "list"), [{"url": "u", "mergeCommit": None}], "has no URL, merge"),
        (True, ("run", "list"), _FAILED, "main CD runs for mmmmmmmmmmmm could not be read"),
        (True, ("run", "list"), {"not": "a list"}, "could not be listed in full"),
        (True, ("run", "list"), [{"databaseId": "x"}], "include an unreadable run"),
        (True, ("run", "view"), _FAILED, "the jobs of CD run 111 could not be read"),
        (True, ("run", "view"), {"jobs": None}, "CD run 111's jobs could not be read"),
        (True, ("run", "view"), {"jobs": [{"name": None}]}, "has an unreadable job"),
        (False, ("issue", "list"), _FAILED, "the release that 2.1.5 superseded could not be"),
        (False, ("issue", "list"), {"not": "a list"}, "could not be searched in full"),
        (False, ("issue", "list"), ["not an issue"], "returned 'not an issue'"),
        (
            False,
            ("issue", "list"),
            [{"number": 39, "title": f"release: {_PRIOR}", "comments": None}],
            "issue 39's comments are None",
        ),
    ],
)
def test_an_unexpected_answer_refuses(
    facts: tuple[_Facts, dict[str, MagicMock]],
    untagged: bool,  # noqa: FBT001 - a parametrized flag
    command: tuple[str, ...],
    answer: object,
    match: str,
) -> None:
    found, _ = facts
    if untagged:
        _untagged(found)
    found.answers[command] = answer
    with pytest.raises(ReleaseError, match=match):
        _rebuild()


def test_a_comment_that_is_not_an_object_does_not_count(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, mocks = facts
    found.closed = [{"number": 39, "title": f"release: {_PRIOR}", "comments": ["text"]}]
    assert _rebuild().issues == _OWN
    mocks["read"].assert_not_called()


@pytest.mark.parametrize(
    ("fails", "match"),
    [(1, "origin/main could not be read"), (2, "tag v2.1.5 could not be read")],
    ids=["fetch-main", "fetch-tag"],
)
def test_a_failed_fetch_refuses(
    facts: tuple[_Facts, dict[str, MagicMock]], fails: int, match: str
) -> None:
    _, mocks = facts
    mocks["run"].side_effect = [*([None] * (fails - 1)), _FAILED]
    with pytest.raises(ReleaseError, match=match):
        _rebuild()


def test_a_failed_git_read_refuses(facts: tuple[_Facts, dict[str, MagicMock]]) -> None:
    with (
        patch(_REBUILD + ".git.read_output", side_effect=_FAILED),
        pytest.raises(ReleaseError, match="`git ls-remote --tags origin refs/tags/v2.1.5`"),
    ):
        _rebuild()


def test_a_breached_superseded_release_with_no_merged_release_pr_refuses(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, mocks = facts
    found.closed = [_prior_issue(_closing_comment("human"))]
    mocks["read"].side_effect = TrackingIssueBreach("ghost", "reopened the issue", number=39)
    with pytest.raises(ReleaseError, match="the release it superseded, 2.1.4, has no merged"):
        _rebuild()


# --- the issues a breached release absorbed from the release it superseded ---


def _prior_issue(*comments: dict[str, Any], title: str = f"release: {_PRIOR}") -> dict[str, Any]:
    return {"number": 39, "title": title, "comments": list(comments)}


def test_absorbed_issues_come_from_the_superseded_release_state(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, mocks = facts
    found.closed = [_prior_issue(_closing_comment("human"))]
    mocks["read"].return_value = MagicMock(issues=_PRIOR_ISSUES)
    assert _rebuild().issues == (
        IssueEntry(f"{_REPO}#10", (7,)),
        IssueEntry(f"{_REPO}#11", (6, 8)),
    )
    ctx = mocks["read"].call_args.args[0]
    assert (ctx.version, ctx.issue_number) == (_PRIOR, 39)


@pytest.mark.parametrize(
    "closed",
    [
        _prior_issue(_closing_comment("agent")),
        _prior_issue(_closing_comment("human", number=41)),
        _prior_issue(_closing_comment("human"), title="release notes"),
    ],
    ids=["agent-comment", "other-issue", "not-a-release"],
)
def test_a_closing_comment_counts_only_from_a_human_for_this_issue(
    facts: tuple[_Facts, dict[str, MagicMock]], closed: dict[str, Any]
) -> None:
    found, mocks = facts
    found.closed = [closed]
    assert _rebuild().issues == _OWN
    mocks["read"].assert_not_called()


def test_two_superseded_releases_refuse(facts: tuple[_Facts, dict[str, MagicMock]]) -> None:
    found, _ = facts
    other = _prior_issue(_closing_comment("human"))
    other["number"] = 38
    found.closed = [_prior_issue(_closing_comment("human")), other]
    with pytest.raises(ReleaseError, match="more than one release says 2.1.5 superseded it"):
        _rebuild()


def test_a_breached_superseded_release_is_rebuilt_too(
    facts: tuple[_Facts, dict[str, MagicMock]],
) -> None:
    found, mocks = facts
    found.closed = [_prior_issue(_closing_comment("human"))]
    found.merged[f"release/{_PRIOR}"] = [_pr(_PRIOR_MERGE, _PRIOR_HEAD, 45)]
    mocks["read"].side_effect = TrackingIssueBreach("ghost", "reopened the issue", number=39)
    prior_own = (IssueEntry(f"{_REPO}#9", (5,)),)

    def issue_list(*, base: str, head: str) -> tuple[IssueEntry, ...]:
        if head == _PRIOR_HEAD:
            # The prior release's own search finds nothing it superseded.
            found.closed = []
            return prior_own
        return _OWN

    mocks["issues"].side_effect = issue_list
    assert _rebuild().issues == (*prior_own, *_OWN)


# --- load_superseded hands a breached issue to the rebuild ---


@pytest.fixture
def loading() -> Iterator[dict[str, MagicMock]]:
    with (
        patch(_SUPERSEDE + "._open_release_issues", return_value=[(_OLD, 40, "no state")]),
        patch(_SUPERSEDE + ".read_state", side_effect=_BREACH),
        patch(_SUPERSEDE + "._found_state") as m_found,
        patch(_SUPERSEDE + ".verify_facts") as m_verify,
        patch(_SUPERSEDE + ".rebuild_breached") as m_rebuild,
        patch(_SUPERSEDE + ".github.read_json", return_value={"tagName": f"v{_OLD}"}) as m_rel,
    ):
        yield {"found": m_found, "verify": m_verify, "rebuild": m_rebuild, "release": m_rel}


def test_load_superseded_rebuilds_a_breached_tagged_release(
    loading: dict[str, MagicMock],
) -> None:
    loading["rebuild"].return_value = Rebuilt(issues=_OWN, main_commit=None, tag=f"v{_OLD}")
    found = load_superseded(_REPO, _ROOT, _OLD, ["prepare"])
    assert found == Superseded(version=_OLD, number=40, issues=_OWN, main_commit=None)
    assert loading["rebuild"].call_args.args == (_REPO, _ROOT, _OLD, 40, _BREACH)
    loading["found"].assert_not_called()
    loading["verify"].assert_not_called()
    assert loading["release"].call_args.args[:3] == ("release", "view", f"v{_OLD}")


def test_load_superseded_rebuilds_a_breached_untagged_release(
    loading: dict[str, MagicMock],
) -> None:
    loading["rebuild"].return_value = Rebuilt(issues=_OWN, main_commit=_MERGE, tag=None)
    found = load_superseded(_REPO, _ROOT, _OLD, ["prepare"])
    assert found.untagged
    assert found.main_commit == _MERGE
    loading["release"].assert_not_called()


def test_load_superseded_refuses_a_breached_release_with_no_github_release(
    loading: dict[str, MagicMock],
) -> None:
    loading["rebuild"].return_value = Rebuilt(issues=_OWN, main_commit=None, tag=f"v{_OLD}")
    loading["release"].return_value = {"tagName": "v0.0.0"}
    with pytest.raises(ReleaseError, match="no GitHub Release of v2.1.5"):
        load_superseded(_REPO, _ROOT, _OLD, ["prepare"])


def test_breach_message_points_at_supersede() -> None:
    message = str(_BREACH)
    assert message.startswith("suspected security breach on tracking issue #40:")
    assert "can never pass this check again" in message
    assert "`vrg-release --supersede <version>`" in message
    assert "clean it up" not in message
