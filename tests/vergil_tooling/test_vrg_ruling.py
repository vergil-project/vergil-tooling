"""Tests for ``vrg-ruling`` request, read and respond, with GitHub faked."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_ruling
from vergil_tooling.lib import github, identity_mode
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.items import SECURITY_REQUEST_TEXT
from vergil_tooling.lib.rulings.marker import Marker, render_marker
from vergil_tooling.lib.trusted_authors import (
    TrustedAuthors,
    TrustedAuthorsNotConfiguredError,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

AGENT = "h-vergil-user"
HUMAN = "h"
AUTHORS = TrustedAuthors(human=(HUMAN,), agents=(AGENT, "h-vergil-audit"))
REQUEST_MARKER = render_marker(Marker("request", None, None))
RESPOND = ("respond", "--issue", "o/r#7", "--request", "41", "--reply", "42", "--items")


def request_comment(cid: int) -> IssueComment:
    body = f"R1.1: fix or file? Recommendation: fix\n\n{REQUEST_MARKER}"
    return IssueComment(cid, AGENT, body, False)


def reply_comment(cid: int, body: str = "R1.1: fix") -> IssueComment:
    return IssueComment(cid, HUMAN, body, False)


def response_comment(cid: int, request_id: int, reply_id: int) -> IssueComment:
    marker = render_marker(Marker("response", request_id, reply_id))
    return IssueComment(cid, AGENT, f"R1.1: fixed\n\n{marker}", False)


class FakeIssue:
    """``o/r#7``: its comments, its POST, and how often each was called."""

    def __init__(self) -> None:
        self.comments: list[IssueComment] = []
        self.posted: list[str] = []
        self.reads = 0
        self.post_error: Exception | None = None
        self.read_error_after_post: Exception | None = None
        self.next_id = 100

    def fetch_comments(self, repo: str, issue: int) -> list[IssueComment]:
        assert (repo, issue) == ("o/r", 7)
        self.reads += 1
        if self.posted and self.read_error_after_post is not None:
            raise self.read_error_after_post
        return list(self.comments)

    def post_json_once(
        self, endpoint: str, body: dict[str, object], *, timeout: float | None = None
    ) -> dict[str, Any]:
        assert endpoint == "repos/o/r/issues/7/comments"
        assert timeout == 30
        text = str(body["body"])
        self.posted.append(text)
        self.comments.append(IssueComment(self.next_id, AGENT, text, False))
        if self.post_error is not None:
            raise self.post_error
        return {"id": self.next_id}


class Cli:
    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._capsys = capsys
        self.stdout = ""
        self.stderr = ""

    def __call__(self, *argv: str) -> int:
        try:
            code = vrg_ruling.main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
        captured = self._capsys.readouterr()
        self.stdout, self.stderr = captured.out, captured.err
        return code


class OrgSpy:
    def __init__(self) -> None:
        self.orgs: set[str | None] = set()
        self.active: list[str | None] = []

    @contextlib.contextmanager
    def __call__(self, org: str | None) -> Generator[None]:
        self.orgs.add(org)
        self.active.append(org)
        try:
            yield
        finally:
            self.active.pop()


@pytest.fixture
def issue(monkeypatch: pytest.MonkeyPatch) -> FakeIssue:
    fake = FakeIssue()
    monkeypatch.setattr("vergil_tooling.lib.rulings.comments.fetch_comments", fake.fetch_comments)
    monkeypatch.setattr(github, "post_json_once", fake.post_json_once)
    monkeypatch.setattr("vergil_tooling.lib.rulings.post.time.sleep", lambda _s: None)
    monkeypatch.setattr(vrg_ruling, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.USER)
    return fake


@pytest.fixture
def target_org_spy(monkeypatch: pytest.MonkeyPatch) -> OrgSpy:
    spy = OrgSpy()
    monkeypatch.setattr(github, "target_org", spy)
    return spy


@pytest.fixture
def cli(issue: FakeIssue, target_org_spy: OrgSpy, capsys: pytest.CaptureFixture[str]) -> Cli:
    del issue, target_org_spy
    return Cli(capsys)


def write_items(tmp_path: Path, items: list[dict[str, Any]], name: str = "items.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(items), encoding="utf-8")
    return str(path)


REQUEST_ITEMS = [
    {"id": "R1.1", "security": False, "text": "fix or file?", "recommendation": "fix"},
    {"id": "R1.S1", "security": True},
]
RESPONSE_ITEMS = [
    {"id": "R1.1", "security": False, "text": "read as fix; fixed"},
    {"id": "R1.S1", "security": True, "outcome": "fixed"},
]


# --- request -----------------------------------------------------------------


def test_request_posts_and_prints_the_id(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    assert cli("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS)) == 0
    assert cli.stdout == "100\n"
    assert issue.posted == [
        "R1.1: fix or file? Recommendation: fix\n"
        f"R1.S1: {SECURITY_REQUEST_TEXT}\n\n{REQUEST_MARKER}"
    ]


def test_request_refuses_open_exchange(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.comments = [request_comment(41)]
    items = write_items(tmp_path, REQUEST_ITEMS)
    assert cli("request", "--issue", "o/r#7", "--items", items) == 1
    assert "open request 41" in cli.stderr
    assert issue.posted == []


def test_request_refuses_a_reply_without_response(
    cli: Cli, issue: FakeIssue, tmp_path: Path
) -> None:
    issue.comments = [request_comment(41), reply_comment(42)]
    assert cli("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS)) == 1
    assert "open request 41" in cli.stderr
    assert issue.posted == []


def test_request_after_a_closed_round(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.comments = [request_comment(41), reply_comment(42), response_comment(43, 41, 42)]
    assert cli("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS)) == 0
    assert len(issue.posted) == 1


def test_request_refuses_bad_items_before_reading(
    cli: Cli, issue: FakeIssue, tmp_path: Path
) -> None:
    items = write_items(tmp_path, [{"id": "R1.S1", "security": True, "text": "detail"}])
    assert cli("request", "--issue", "o/r#7", "--items", items) == 1
    assert "R1.S1" in cli.stderr
    assert issue.reads == 0
    assert issue.posted == []


def test_request_outcome_unknown_then_rerun_names_it(
    cli: Cli, issue: FakeIssue, tmp_path: Path
) -> None:
    items = write_items(tmp_path, REQUEST_ITEMS)
    issue.post_error = github.GitHubAPIError(1, ("gh",), None, "HTTP 502")
    issue.read_error_after_post = github.GitHubAPIError(1, ("gh",), None, "HTTP 502")
    assert cli("request", "--issue", "o/r#7", "--items", items) == 1
    assert "could not tell whether the comment landed" in cli.stderr
    assert "Re-run vrg-ruling request" in cli.stderr
    assert len(issue.posted) == 1
    issue.read_error_after_post = None
    assert cli("request", "--issue", "o/r#7", "--items", items) == 1
    assert "open request 100" in cli.stderr
    assert len(issue.posted) == 1


def test_request_landed_despite_502(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.post_error = github.GitHubAPIError(1, ("gh",), None, "HTTP 502")
    assert cli("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS)) == 0
    assert cli.stdout == "100\n"
    assert len(issue.posted) == 1


@pytest.mark.parametrize(
    "rendered",
    [
        f"{REQUEST_MARKER}\nR1.1: x\n\n{REQUEST_MARKER}",
        f"R1.1: x\n{REQUEST_MARKER}x\n\n{REQUEST_MARKER}",
        f"R1.1: café\n\n{REQUEST_MARKER}",
        "R1.1: x\n\n<!-- vrg-ruling kind=response request=1 reply=2 -->",
        "R1.1: x",
    ],
)
def test_rendered_request_round_trips(
    cli: Cli,
    issue: FakeIssue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rendered: str,
) -> None:
    monkeypatch.setattr(vrg_ruling, "render_request", lambda _items: rendered)
    assert cli("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS)) == 1
    assert "nothing was posted" in cli.stderr or "not plain text" in cli.stderr
    assert issue.posted == []


# --- read --------------------------------------------------------------------


def test_read_valid_reply(cli: Cli, issue: FakeIssue) -> None:
    issue.comments = [request_comment(41), reply_comment(42, "R1.1: fix\nR1.S1: defer")]
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 0
    assert cli.stdout == "R1.1: fix\nR1.S1: defer\n"
    assert "reply 42 to request 41" in cli.stderr


def test_read_no_reply_yet(cli: Cli, issue: FakeIssue) -> None:
    issue.comments = [request_comment(41), IssueComment(50, "spammer", "buy now", False)]
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 3
    assert cli.stdout == ""
    assert "no reply yet to request 41" in cli.stderr


@pytest.mark.parametrize(
    ("comments", "check", "comment_id"),
    [
        ([request_comment(41), IssueComment(42, AGENT, "note", False)], "turn", 42),
        ([request_comment(41), reply_comment(42), reply_comment(43)], "turn", 43),
        ([request_comment(41), reply_comment(42, "fix‮")], "reply-text", 42),
        ([IssueComment(41, AGENT, "R1\n\n" + REQUEST_MARKER, True)], "edited", 41),
        ([reply_comment(1)], "deleted", 41),
        ([IssueComment(41, HUMAN, REQUEST_MARKER, False)], "request-author", 41),
    ],
)
def test_read_fatal_checks(
    cli: Cli, issue: FakeIssue, comments: list[IssueComment], check: str, comment_id: int
) -> None:
    issue.comments = comments
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 4
    assert f"fatal check '{check}' on comment {comment_id}" in cli.stderr
    assert cli.stdout == ""


def test_read_untrusted_request_is_request_author(cli: Cli, issue: FakeIssue) -> None:
    issue.comments = [IssueComment(41, "stranger", REQUEST_MARKER, False)]
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 4
    assert "fatal check 'request-author' on comment 41" in cli.stderr


@pytest.mark.parametrize(
    "argv",
    [
        ("read", "--issue", "o/r#7"),
        ("read", "--issue", "o/r#7", "--request", "0"),
        ("read", "--issue", "o/r#7", "--request", "x1"),
        ("respond", "--issue", "o/r#7", "--request", "41", "--items", "i.json"),
        ("request", "--issue", "o/r#7"),
        (),
    ],
)
def test_usage_errors_exit_2(cli: Cli, issue: FakeIssue, argv: tuple[str, ...]) -> None:
    assert cli(*argv) == 2
    assert issue.reads == 0


def test_bad_issue_ref_exits_1(cli: Cli, issue: FakeIssue) -> None:
    assert cli("read", "--issue", "o/r", "--request", "41") == 1
    assert "owner/repo#N" in cli.stderr
    assert issue.reads == 0


# --- respond -----------------------------------------------------------------


def test_respond_records_ids(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    after: list[tuple[str, int, int]] = []
    monkeypatch.setattr(vrg_ruling, "_after_respond", lambda *a: after.append(a))
    issue.comments = [request_comment(41), reply_comment(42)]
    items = write_items(tmp_path, RESPONSE_ITEMS)
    assert cli(*RESPOND, items) == 0
    assert cli.stdout == "100\n"
    assert len(issue.posted) == 1
    body = issue.posted[0]
    assert body.endswith(render_marker(Marker("response", 41, 42)))
    assert body.splitlines()[:2] == ["R1.1: read as fix; fixed", "R1.S1: fixed"]
    assert after == [("o/r", 7, 41)]


def test_respond_rerun_finds_landed_response(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    after: list[tuple[str, int, int]] = []
    monkeypatch.setattr(vrg_ruling, "_after_respond", lambda *a: after.append(a))
    issue.comments = [request_comment(41), reply_comment(42), response_comment(43, 41, 42)]
    items = write_items(tmp_path, RESPONSE_ITEMS)
    assert cli(*RESPOND, items) == 0
    assert cli.stdout == "43\n"
    assert "already landed" in cli.stderr
    assert issue.posted == []
    assert after == [("o/r", 7, 41)]


def test_respond_outcome_unknown_then_rerun_succeeds(
    cli: Cli, issue: FakeIssue, tmp_path: Path
) -> None:
    issue.comments = [request_comment(41), reply_comment(42)]
    issue.post_error = github.GitHubAPIError(1, ("gh",), None, "HTTP 504")
    issue.read_error_after_post = github.GitHubAPIError(1, ("gh",), None, "HTTP 502")
    items = write_items(tmp_path, RESPONSE_ITEMS)
    argv = (*RESPOND, items)
    assert cli(*argv) == 1
    assert "could not tell" in cli.stderr
    assert "Re-run vrg-ruling respond" in cli.stderr
    issue.read_error_after_post = None
    assert cli(*argv) == 0
    assert cli.stdout == "100\n"
    assert len(issue.posted) == 1


def test_respond_not_landed_exits_1(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue.comments = [request_comment(41), reply_comment(42)]
    calls: list[str] = []

    def fail(endpoint: str, _body: object, **_k: object) -> dict[str, Any]:
        calls.append(endpoint)
        raise github.GitHubAPIError(1, ("gh",), None, "HTTP 502")

    monkeypatch.setattr(github, "post_json_once", fail)
    items = write_items(tmp_path, RESPONSE_ITEMS)
    argv = (*RESPOND, items)
    assert cli(*argv) == 1
    assert len(calls) == 1
    assert "did not land" in cli.stderr
    assert "Re-running the response is safe" in cli.stderr


@pytest.mark.parametrize(
    ("comments", "check", "comment_id"),
    [
        ([request_comment(41), reply_comment(42), reply_comment(43)], "turn", 43),
        ([request_comment(41)], "deleted", 42),
        ([request_comment(41), reply_comment(44)], "deleted", 42),
        ([request_comment(41), reply_comment(42), response_comment(43, 41, 99)], "turn", 43),
        ([request_comment(41), IssueComment(42, HUMAN, "fix", True)], "edited", 42),
        ([reply_comment(42)], "deleted", 41),
    ],
)
def test_respond_fatal_checks(
    cli: Cli,
    issue: FakeIssue,
    tmp_path: Path,
    comments: list[IssueComment],
    check: str,
    comment_id: int,
) -> None:
    issue.comments = comments
    items = write_items(tmp_path, RESPONSE_ITEMS)
    assert cli(*RESPOND, items) == 4
    assert f"fatal check '{check}' on comment {comment_id}" in cli.stderr
    assert issue.posted == []


def test_respond_refuses_flagged_detail(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.comments = [request_comment(41), reply_comment(42)]
    items = write_items(
        tmp_path, [{"id": "R1.S1", "security": True, "outcome": "fixed in src/auth.py"}]
    )
    assert cli(*RESPOND, items) == 1
    assert "R1.S1" in cli.stderr
    assert "src/auth.py" not in cli.stderr
    assert issue.posted == []


@pytest.mark.parametrize(
    "rendered",
    [
        f"{REQUEST_MARKER}\nR1.1: x\n\n" + render_marker(Marker("response", 41, 42)),
        "R1.1: x\n\n" + render_marker(Marker("response", 41, 43)),
        "R1.1: a\tb\n\n" + render_marker(Marker("response", 41, 42)),
    ],
)
def test_rendered_response_round_trips(
    cli: Cli,
    issue: FakeIssue,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rendered: str,
) -> None:
    issue.comments = [request_comment(41), reply_comment(42)]
    monkeypatch.setattr(vrg_ruling, "render_response", lambda _items, **_k: rendered)
    items = write_items(tmp_path, RESPONSE_ITEMS)
    assert cli(*RESPOND, items) == 1
    assert issue.posted == []


# --- identity and org ------------------------------------------------------


@pytest.mark.parametrize("mode", [IdentityMode.AUDIT, IdentityMode.HUMAN])
def test_refuses_outside_user_mode(
    cli: Cli, issue: FakeIssue, monkeypatch: pytest.MonkeyPatch, mode: IdentityMode
) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: mode)
    issue.comments = [request_comment(41), reply_comment(42)]
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 1
    assert f"'{mode.value}' mode" in cli.stderr
    assert issue.reads == 0


def test_refuses_unlisted_agent_login(
    cli: Cli, issue: FakeIssue, monkeypatch: pytest.MonkeyPatch
) -> None:
    authors = TrustedAuthors(human=(HUMAN,), agents=("someone-else",))
    monkeypatch.setattr(vrg_ruling, "load_trusted_authors", lambda: authors)
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 1
    assert "h-vergil-user" in cli.stderr
    assert issue.reads == 0


def test_refuses_two_human_logins(
    cli: Cli, issue: FakeIssue, monkeypatch: pytest.MonkeyPatch
) -> None:
    authors = TrustedAuthors(human=(HUMAN, "h2"), agents=(AGENT,))
    monkeypatch.setattr(vrg_ruling, "load_trusted_authors", lambda: authors)
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 1
    assert "exactly one human login" in cli.stderr
    assert issue.reads == 0


@pytest.mark.parametrize(
    "error",
    [TrustedAuthorsNotConfiguredError("not configured"), ValueError("bad trusted authors")],
)
def test_refuses_without_trusted_authors(
    cli: Cli, issue: FakeIssue, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def load() -> TrustedAuthors:
        raise error

    monkeypatch.setattr(vrg_ruling, "load_trusted_authors", load)
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 1
    assert str(error) in cli.stderr
    assert issue.reads == 0


def test_uses_issue_owner_org(
    cli: Cli, issue: FakeIssue, target_org_spy: OrgSpy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github, "detect_org", lambda **_k: "other")
    seen: list[list[str | None]] = []
    real_fetch = issue.fetch_comments

    def fetch(repo: str, number: int) -> list[IssueComment]:
        seen.append(list(target_org_spy.active))
        return real_fetch(repo, number)

    monkeypatch.setattr("vergil_tooling.lib.rulings.comments.fetch_comments", fetch)
    issue.comments = [request_comment(41)]
    cli("read", "--issue", "o/r#7", "--request", "41")
    assert target_org_spy.orgs == {"o"}
    assert seen == [["o"]]


def test_post_runs_inside_the_issue_owner_org(
    cli: Cli,
    issue: FakeIssue,
    target_org_spy: OrgSpy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active: list[list[str | None]] = []
    real_post = issue.post_json_once

    def post(endpoint: str, body: dict[str, object], *, timeout: float | None = None) -> Any:
        active.append(list(target_org_spy.active))
        return real_post(endpoint, body, timeout=timeout)

    monkeypatch.setattr(github, "post_json_once", post)
    assert cli("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS)) == 0
    assert active == [["o"]]


def test_github_failure_reading_exits_1(
    cli: Cli, issue: FakeIssue, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_repo: str, _issue: int) -> list[IssueComment]:
        raise github.GitHubAPIError(1, ("gh",), None, "HTTP 500")

    monkeypatch.setattr("vergil_tooling.lib.rulings.comments.fetch_comments", fail)
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 1
    assert "HTTP 500" in cli.stderr


def test_request_not_landed_exits_1(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fail(endpoint: str, _body: object, **_k: object) -> dict[str, Any]:
        calls.append(endpoint)
        raise github.GitHubAPIError(1, ("gh",), None, "HTTP 502")

    monkeypatch.setattr(github, "post_json_once", fail)
    assert cli("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS)) == 1
    assert len(calls) == 1
    assert "did not land" in cli.stderr
    assert "Re-running the request is safe" in cli.stderr
    assert issue.posted == []


def test_missing_token_reaches_the_cli_boundary(
    cli: Cli, issue: FakeIssue, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_token(_repo: str, _issue: int) -> list[IssueComment]:
        raise github.MissingGitHubTokenError(4, ("gh",), None, "no token")

    monkeypatch.setattr("vergil_tooling.lib.rulings.comments.fetch_comments", no_token)
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 1
    assert "GH_TOKEN" in cli.stderr or "token" in cli.stderr.lower()
    assert issue.posted == []


def test_after_respond_is_a_no_op() -> None:
    assert vrg_ruling._after_respond("o/r", 7, 41) is None
