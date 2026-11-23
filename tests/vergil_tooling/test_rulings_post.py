"""Tests for ``vergil_tooling.lib.rulings.post``: every comment is posted once."""

from __future__ import annotations

from typing import Any

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import github, trust
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.post import (
    POST_TIMEOUT,
    REREADS,
    PostNotLanded,
    PostOutcomeUnknown,
    post_comment_once,
)
from vergil_tooling.lib.trusted_authors import TrustedAuthors

AGENT = "h-vergil-user"
AUTHORS = TrustedAuthors(human=("h",), agents=(AGENT,))


def _node(comment: IssueComment) -> dict[str, Any]:
    """The trust layer's comment node for *comment*: an agent is a Bot, anyone else a User."""
    typename = "Bot" if comment.author in AUTHORS.agents else "User"
    return trust_stubs.comment(
        comment.id, comment.body, {"__typename": typename, "login": comment.author}
    )


def _api_error(status: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "api"), None, f"HTTP {status}")


class FakeGitHub:
    """A fake issue: the comments it holds, and how its POST and reads behave."""

    def __init__(self) -> None:
        self.comments: list[IssueComment] = [IssueComment(10, "h", "earlier", edited=False)]
        self.post_calls = 0
        self.post_timeout: float | None = None
        self.post_bodies: list[dict[str, object]] = []
        self._post_error: Exception | None = None
        self._post_reply: dict[str, Any] = {"id": 99}
        self._landed: IssueComment | None = None
        self._read_errors: list[Exception | None] = []
        self.reads = 0
        self.sleeps: list[float] = []

    # --- configuration ---
    def post_fails_with(self, status: int) -> None:
        self._post_error = _api_error(f"{status} Bad Gateway")

    def post_hangs(self) -> None:
        self._post_error = github.GitHubAPIError(1, ("gh", "api"), None, "timed out")

    def post_answers(self, payload: dict[str, Any]) -> None:
        self._post_reply = payload

    def comment_landed(self, *, body: str, author: str, id: int) -> None:  # noqa: A002
        self._landed = IssueComment(id, author, body, edited=False)

    def reads_fail_with(self, status: int) -> None:
        # The snapshot read succeeds; every re-read fails.
        self._read_errors = [None] + [_api_error(str(status))] * REREADS

    def reads_after_snapshot(self, *outcomes: Exception | None) -> None:
        self._read_errors = [None, *outcomes]

    def snapshot_fails(self) -> None:
        self._read_errors = [_api_error("502")]

    # --- fakes ---
    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        assert query == trust._ITEM_QUERY
        assert variables == {"owner": "o", "name": "r", "number": 7}
        index = self.reads
        self.reads += 1
        error = self._read_errors[index] if index < len(self._read_errors) else None
        if error is not None:
            raise error
        held = list(self.comments)
        if index > 0 and self._landed is not None:
            held.append(self._landed)
        return trust_stubs.item(nodes=[_node(c) for c in held])

    def post_json_once(
        self, endpoint: str, body: dict[str, object], *, timeout: float | None = None
    ) -> dict[str, Any]:
        assert endpoint == "repos/o/r/issues/7/comments"
        self.post_calls += 1
        self.post_timeout = timeout
        self.post_bodies.append(body)
        if self._post_error is not None:
            raise self._post_error
        return self._post_reply


@pytest.fixture
def fake_gh(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(github, "graphql", fake.graphql)
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(github, "post_json_once", fake.post_json_once)
    monkeypatch.setattr("vergil_tooling.lib.rulings.post.time.sleep", fake.sleeps.append)
    return fake


def test_post_once_returns_the_new_id(fake_gh: FakeGitHub) -> None:
    assert post_comment_once("o/r", 7, "B", author=AGENT) == 99
    assert fake_gh.post_calls == 1
    assert fake_gh.post_bodies == [{"body": "B"}]
    assert fake_gh.post_timeout == POST_TIMEOUT == 30
    assert fake_gh.sleeps == []


def test_post_once_never_retries(fake_gh: FakeGitHub) -> None:
    fake_gh.post_fails_with(502)
    fake_gh.comment_landed(body="B", author="h-vergil-user", id=55)
    assert post_comment_once("o/r", 7, "B", author="h-vergil-user") == 55
    assert fake_gh.post_calls == 1


def test_post_once_times_out_into_reread(fake_gh: FakeGitHub) -> None:
    fake_gh.post_hangs()
    fake_gh.comment_landed(body="B", author="h-vergil-user", id=55)
    assert post_comment_once("o/r", 7, "B", author="h-vergil-user") == 55
    assert fake_gh.post_calls == 1
    assert fake_gh.post_timeout == 30


def test_post_once_reports_not_landed(fake_gh: FakeGitHub) -> None:
    fake_gh.post_fails_with(502)
    with pytest.raises(PostNotLanded, match="did not land on o/r#7; nothing more was posted"):
        post_comment_once("o/r", 7, "B", author="h-vergil-user")
    assert fake_gh.post_calls == 1
    assert fake_gh.reads == 1 + REREADS
    assert fake_gh.sleeps == [2.0] * REREADS


def test_post_once_outcome_unknown(fake_gh: FakeGitHub) -> None:
    fake_gh.post_fails_with(502)
    fake_gh.reads_fail_with(502)
    with pytest.raises(PostOutcomeUnknown, match="could not tell") as excinfo:
        post_comment_once("o/r", 7, "B", author="h-vergil-user")
    assert fake_gh.post_calls == 1
    assert len(excinfo.value.errors) == REREADS
    assert "nothing more was posted" in str(excinfo.value)


def test_one_successful_reread_is_enough_to_say_not_landed(fake_gh: FakeGitHub) -> None:
    fake_gh.post_fails_with(502)
    fake_gh.reads_after_snapshot(_api_error("502"), None, _api_error("502"))
    with pytest.raises(PostNotLanded):
        post_comment_once("o/r", 7, "B", author=AGENT)
    assert fake_gh.post_calls == 1


def test_a_later_reread_finds_a_lagging_comment(fake_gh: FakeGitHub) -> None:
    fake_gh.post_fails_with(502)
    fake_gh.comment_landed(body="B", author=AGENT, id=55)
    fake_gh.reads_after_snapshot(_api_error("502"), _api_error("502"))
    assert post_comment_once("o/r", 7, "B", author=AGENT) == 55
    assert fake_gh.post_calls == 1


@pytest.mark.parametrize(
    "landed",
    [
        IssueComment(55, "someone-else", "B", edited=False),
        IssueComment(55, AGENT, "B but different", edited=False),
        IssueComment(10, AGENT, "B", edited=False),  # already there before the post
    ],
)
def test_only_a_new_matching_comment_counts(fake_gh: FakeGitHub, landed: IssueComment) -> None:
    fake_gh.post_fails_with(502)
    fake_gh.comment_landed(body=landed.body, author=landed.author, id=landed.id)
    if landed.id == 10:
        fake_gh.comments = [landed]
    with pytest.raises(PostNotLanded):
        post_comment_once("o/r", 7, "B", author=AGENT)
    assert fake_gh.post_calls == 1


def test_a_success_without_an_id_is_rechecked(fake_gh: FakeGitHub) -> None:
    fake_gh.post_answers({"id": True})
    fake_gh.comment_landed(body="B", author=AGENT, id=55)
    assert post_comment_once("o/r", 7, "B", author=AGENT) == 55
    assert fake_gh.post_calls == 1


def test_missing_token_is_raised_without_rereads(
    fake_gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_token(*_args: object, **_kwargs: object) -> dict[str, Any]:
        raise github.MissingGitHubTokenError(4, ("gh",), None, "no token")

    monkeypatch.setattr(github, "post_json_once", no_token)
    with pytest.raises(github.MissingGitHubTokenError):
        post_comment_once("o/r", 7, "B", author=AGENT)
    assert fake_gh.reads == 1


def test_snapshot_failure_posts_nothing(fake_gh: FakeGitHub) -> None:
    fake_gh.snapshot_fails()
    with pytest.raises(trust.TrustReadError, match="HTTP 502"):
        post_comment_once("o/r", 7, "B", author=AGENT)
    assert fake_gh.post_calls == 0


def test_the_passed_authors_judge_the_rereads(
    fake_gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_loader() -> TrustedAuthors:
        raise AssertionError("the passed authors are used, not the loaded ones")

    monkeypatch.setattr(trust, "load_trusted_authors", no_loader)
    fake_gh.post_fails_with(502)
    fake_gh.comment_landed(body="B", author=AGENT, id=55)
    assert post_comment_once("o/r", 7, "B", author=AGENT, authors=AUTHORS) == 55
    assert fake_gh.post_calls == 1


def test_a_landed_comment_by_a_user_named_like_the_agent_does_not_count(
    fake_gh: FakeGitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    text = "R1.1: fixed as asked"
    forged = trust_stubs.comment(55, text, {"__typename": "User", "login": AGENT})
    real = fake_gh.graphql

    def graphql(query: str, **variables: object) -> dict[str, Any]:
        answer = real(query, **variables)
        if fake_gh.reads > 1:
            answer["repository"]["issueOrPullRequest"]["comments"]["nodes"].append(forged)
        return answer

    monkeypatch.setattr(github, "graphql", graphql)
    fake_gh.post_fails_with(502)
    with pytest.raises(PostNotLanded) as excinfo:
        post_comment_once("o/r", 7, text, author=AGENT)
    assert fake_gh.post_calls == 1
    captured = capsys.readouterr()
    err = captured.err
    assert text not in captured.out + err + str(excinfo.value)
    assert (
        f"skipped untrusted content: o/r#7 comment {AGENT} at {trust_stubs.CREATED}"
        " on comment 55 (wrong-type)\n"
    ) in err
