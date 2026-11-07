"""Tests for ``vrg-ruling`` request, read, draft, draft-scope and respond, with GitHub faked."""

from __future__ import annotations

import contextlib
import json
import subprocess
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
        self.drafts: dict[str, str] = {}
        self.deleted: list[str] = []
        self.delete_error: Exception | None = None

    def delete_if_exists(self, endpoint: str) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(endpoint)
        return self.drafts.pop(endpoint.removeprefix("repos/o/r/git/"), None) is not None

    def push_draft(self, issue: int, request_id: int, text: str, *, remote: str = "origin") -> None:
        assert remote == "origin"
        self.drafts[f"refs/vergil/ruling-draft/{issue}/{request_id}"] = text

    def push_scope_draft(self, issue: int, text: str, *, remote: str = "origin") -> None:
        assert remote == "origin"
        self.drafts[f"refs/vergil/scope-draft/{issue}"] = text

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
    monkeypatch.setattr(github, "delete_if_exists", fake.delete_if_exists)
    monkeypatch.setattr("vergil_tooling.lib.rulings.draft_ref.push_draft", fake.push_draft)
    monkeypatch.setattr(
        "vergil_tooling.lib.rulings.draft_ref.push_scope_draft", fake.push_scope_draft
    )
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
    assert cli.stderr == (
        "vrg-ruling: o/r#7 has open request 41; read it with"
        " vrg-ruling read --issue 'o/r#7' --request 41\n"
    )
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


def test_read_json_valid_reply(cli: Cli, issue: FakeIssue) -> None:
    issue.comments = [request_comment(41), reply_comment(42, "R1.1: fix\nR1.S1: defer")]
    assert cli("read", "--issue", "o/r#7", "--request", "41", "--json") == 0
    assert json.loads(cli.stdout) == {
        "request": 41,
        "reply": 42,
        "body": "R1.1: fix\nR1.S1: defer",
    }
    assert cli.stdout.count("\n") == 1
    assert "reply 42 to request 41" in cli.stderr


@pytest.mark.parametrize(
    ("comments", "code", "message"),
    [
        ([request_comment(41)], 3, "no reply yet to request 41"),
        (
            [request_comment(41), reply_comment(42), reply_comment(43)],
            4,
            "fatal check 'turn' on comment 43",
        ),
    ],
)
def test_read_json_no_reply_and_fatal(
    cli: Cli, issue: FakeIssue, comments: list[IssueComment], code: int, message: str
) -> None:
    issue.comments = comments
    assert cli("read", "--issue", "o/r#7", "--request", "41", "--json") == code
    assert cli.stdout == ""
    assert message in cli.stderr


def test_read_without_json_unchanged(cli: Cli, issue: FakeIssue) -> None:
    issue.comments = [request_comment(41), reply_comment(42, "R1.1: fix")]
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 0
    assert cli.stdout == "R1.1: fix\n"
    assert cli.stderr == "vrg-ruling: reply 42 to request 41 on o/r#7\n"


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


# --- draft ---------------------------------------------------------------------

DRAFT_REF = "refs/vergil/ruling-draft/7/41"
REPLY_ITEMS = [
    {"id": "R1.1", "security": False, "text": "file it"},
    {"id": "R1.S1", "security": True, "decision": "fix"},
]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(("git", *args), cwd=cwd, check=True, capture_output=True)  # noqa: S603, S607


def origin_is(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, repo: str) -> None:
    """Stand in a fresh clone whose ``origin`` is ``repo`` on github.com."""
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    clone = tmp_path / "clone"
    _git("init", str(clone), cwd=tmp_path)
    _git("remote", "add", "origin", f"https://github.com/{repo}.git", cwd=clone)
    monkeypatch.chdir(clone)


@pytest.fixture
def in_clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    origin_is(tmp_path, monkeypatch, "o/r")


def draft_argv(items: str, request: str = "41") -> tuple[str, ...]:
    return ("draft", "--issue", "o/r#7", "--request", request, "--items", items)


@pytest.mark.usefixtures("in_clone")
def test_draft_prints_reply_command(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.comments = [request_comment(41)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 0
    assert cli.stdout == "vrg-ruling-reply --issue 'o/r#7' --request 41\n"
    assert DRAFT_REF in cli.stderr
    assert issue.drafts == {DRAFT_REF: "R1.1: file it\nR1.S1: fix\n"}
    assert issue.posted == []


def test_draft_refuses_other_repo(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin_is(tmp_path, monkeypatch, "o/other")
    issue.comments = [request_comment(41)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 1
    assert "o/other" in cli.stderr
    assert "clone of o/r" in cli.stderr
    assert issue.drafts == {}
    assert issue.reads == 0


def test_draft_refuses_outside_a_clone(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "plain").mkdir()
    monkeypatch.chdir(tmp_path / "plain")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    issue.comments = [request_comment(41)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 1
    assert "cannot read the push URL" in cli.stderr
    assert issue.drafts == {}


@pytest.mark.usefixtures("in_clone")
@pytest.mark.parametrize("mode", [IdentityMode.AUDIT, IdentityMode.HUMAN])
def test_draft_refuses_in_audit_mode(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: IdentityMode
) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: mode)
    issue.comments = [request_comment(41)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 1
    assert issue.drafts == {}
    assert issue.reads == 0


@pytest.mark.usefixtures("in_clone")
def test_draft_refuses_unlisted_login(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authors = TrustedAuthors(human=(HUMAN,), agents=("someone-else",))
    monkeypatch.setattr(vrg_ruling, "load_trusted_authors", lambda: authors)
    issue.comments = [request_comment(41)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 1
    assert issue.drafts == {}
    assert issue.reads == 0


@pytest.mark.usefixtures("in_clone")
def test_draft_refuses_answered_request(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.comments = [request_comment(41), reply_comment(42)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 1
    assert "already has reply 42" in cli.stderr
    assert issue.drafts == {}


@pytest.mark.usefixtures("in_clone")
def test_draft_fatal_check_pushes_nothing(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.comments = [IssueComment(41, AGENT, request_comment(41).body, True)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 4
    assert "fatal check 'edited'" in cli.stderr
    assert issue.drafts == {}


@pytest.mark.usefixtures("in_clone")
@pytest.mark.parametrize(
    "items",
    [
        [{"id": "R1.S1", "security": True, "decision": "fixed"}],
        [{"id": "R1.S1", "security": True, "decision": "fix the SQL in x"}],
        [{"id": "R1.S1", "security": True, "decision": "fix", "text": "the SQL in x"}],
        [{"id": "R1.1", "security": False, "text": "fix\u00e9"}],
        [{"id": "R1.1", "security": False, "text": "fix <!-- vrg-ruling"}],
    ],
)
def test_draft_refuses_bad_items_before_reading(
    cli: Cli, issue: FakeIssue, tmp_path: Path, items: list[dict[str, Any]]
) -> None:
    issue.comments = [request_comment(41)]
    assert cli(*draft_argv(write_items(tmp_path, items))) == 1
    assert items[0]["id"] in cli.stderr
    assert "SQL in x" not in cli.stderr
    assert issue.reads == 0
    assert issue.drafts == {}


@pytest.mark.usefixtures("in_clone")
def test_draft_runs_inside_the_issue_owner_org(
    cli: Cli,
    issue: FakeIssue,
    target_org_spy: OrgSpy,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active: list[list[str | None]] = []

    def push(issue_n: int, request_id: int, text: str, *, remote: str = "origin") -> None:
        active.append(list(target_org_spy.active))
        issue.push_draft(issue_n, request_id, text, remote=remote)

    monkeypatch.setattr("vergil_tooling.lib.rulings.draft_ref.push_draft", push)
    issue.comments = [request_comment(41)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 0
    assert active == [["o"]]


@pytest.mark.usefixtures("in_clone")
def test_draft_push_failure_exits_1(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*_a: object, **_k: object) -> None:
        msg = "pushing the draft to refs/vergil/ruling-draft/7/41 failed: git exited 128"
        raise vrg_ruling.RulingError(msg)

    monkeypatch.setattr("vergil_tooling.lib.rulings.draft_ref.push_draft", fail)
    issue.comments = [request_comment(41)]
    assert cli(*draft_argv(write_items(tmp_path, REPLY_ITEMS))) == 1
    assert "git exited 128" in cli.stderr
    assert cli.stdout == ""


# --- draft-scope ---------------------------------------------------------------

SCOPE_REF = "refs/vergil/scope-draft/7"


def scope_argv(tmp_path: Path, text: str) -> tuple[str, ...]:
    path = tmp_path / "scope.txt"
    path.write_text(text, encoding="utf-8")
    return ("draft-scope", "--issue", "o/r#7", "--text-file", str(path))


@pytest.mark.usefixtures("in_clone")
def test_draft_scope_prints_the_scope_command(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    assert cli(*scope_argv(tmp_path, "Also cover the docs.\n")) == 0
    assert cli.stdout == "vrg-scope-change --issue 'o/r#7'\n"
    assert SCOPE_REF in cli.stderr
    assert issue.drafts == {SCOPE_REF: "Also cover the docs.\n"}
    assert issue.reads == 1
    assert issue.posted == []


@pytest.mark.usefixtures("in_clone")
def test_draft_scope_does_not_check_the_turn(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    # vrg-scope-change refuses while the exchange is open, when the human runs it.
    issue.comments = [request_comment(41)]
    assert cli(*scope_argv(tmp_path, "Narrow it to x.\n")) == 0
    assert issue.drafts == {SCOPE_REF: "Narrow it to x.\n"}


@pytest.mark.usefixtures("in_clone")
@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("caf\u00e9\n", "not plain text"),
        ("a\tb\n", "not plain text"),
        ("x <!-- vrg-ruling kind=request -->\n", "marker prefix"),
        ("\n \n", "blank"),
        ("Scope change\n\nx\n", "header is added for you"),
    ],
)
def test_draft_scope_refuses_bad_text_before_reading(
    cli: Cli, issue: FakeIssue, tmp_path: Path, text: str, match: str
) -> None:
    assert cli(*scope_argv(tmp_path, text)) == 1
    assert match in cli.stderr
    assert issue.reads == 0
    assert issue.drafts == {}


@pytest.mark.usefixtures("in_clone")
def test_draft_scope_refuses_an_unreadable_file(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    missing = str(tmp_path / "missing.txt")
    assert cli("draft-scope", "--issue", "o/r#7", "--text-file", missing) == 1
    assert "cannot read the scope change file" in cli.stderr
    assert issue.drafts == {}


def test_draft_scope_refuses_other_repo(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin_is(tmp_path, monkeypatch, "o/other")
    assert cli(*scope_argv(tmp_path, "x\n")) == 1
    assert "run vrg-ruling draft-scope in a clone of o/r" in cli.stderr
    assert issue.drafts == {}
    assert issue.reads == 0


@pytest.mark.usefixtures("in_clone")
@pytest.mark.parametrize("mode", [IdentityMode.AUDIT, IdentityMode.HUMAN])
def test_draft_scope_refuses_outside_user_mode(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: IdentityMode
) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: mode)
    assert cli(*scope_argv(tmp_path, "x\n")) == 1
    assert issue.drafts == {}


@pytest.mark.usefixtures("in_clone")
def test_draft_scope_refuses_a_missing_issue(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def not_found(_repo: str, _issue: int) -> list[IssueComment]:
        raise vrg_ruling.RulingError("o/r#7: issue not found")

    monkeypatch.setattr("vergil_tooling.lib.rulings.comments.fetch_comments", not_found)
    assert cli(*scope_argv(tmp_path, "x\n")) == 1
    assert "issue not found" in cli.stderr
    assert issue.drafts == {}


# --- respond deletes the draft -----------------------------------------------------


def test_respond_deletes_leftover_draft(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.drafts = {DRAFT_REF: "R1.1: fix\n"}
    issue.comments = [request_comment(41), reply_comment(42)]
    assert cli(*RESPOND, write_items(tmp_path, RESPONSE_ITEMS)) == 0
    assert issue.deleted == ["repos/o/r/git/refs/vergil/ruling-draft/7/41"]
    assert issue.drafts == {}


def test_respond_absent_draft_is_not_an_error(cli: Cli, issue: FakeIssue, tmp_path: Path) -> None:
    issue.comments = [request_comment(41), reply_comment(42)]
    assert cli(*RESPOND, write_items(tmp_path, RESPONSE_ITEMS)) == 0
    assert issue.deleted == ["repos/o/r/git/refs/vergil/ruling-draft/7/41"]


def test_respond_absent_draft_422_is_not_an_error(
    cli: Cli, issue: FakeIssue, tmp_path: Path
) -> None:
    issue.delete_error = github.GitHubAPIError(
        1, ("gh",), None, "gh: Reference does not exist (HTTP 422)\n"
    )
    issue.comments = [request_comment(41), reply_comment(42)]
    assert cli(*RESPOND, write_items(tmp_path, RESPONSE_ITEMS)) == 0
    assert cli.stdout == "100\n"


def test_respond_delete_failure_says_the_response_landed(
    cli: Cli, issue: FakeIssue, tmp_path: Path
) -> None:
    issue.delete_error = github.GitHubAPIError(1, ("gh",), None, "gh: Server Error (HTTP 500)\n")
    issue.comments = [request_comment(41), reply_comment(42)]
    assert cli(*RESPOND, write_items(tmp_path, RESPONSE_ITEMS)) == 1
    assert cli.stdout == "100\n"
    assert "the response is posted" in cli.stderr
    assert "posts nothing" in cli.stderr
    assert len(issue.posted) == 1


def test_respond_delete_missing_token_reaches_the_cli_boundary(
    cli: Cli, issue: FakeIssue, tmp_path: Path
) -> None:
    issue.delete_error = github.MissingGitHubTokenError(4, ("gh",), None, "no token")
    issue.comments = [request_comment(41), reply_comment(42)]
    assert cli(*RESPOND, write_items(tmp_path, RESPONSE_ITEMS)) == 1
    assert "token" in cli.stderr.lower()
    assert "the response is posted" not in cli.stderr


def test_respond_deletes_by_the_issue_repo_whatever_origin(
    cli: Cli, issue: FakeIssue, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    origin_is(tmp_path, monkeypatch, "x/elsewhere")
    issue.comments = [request_comment(41), reply_comment(42), response_comment(43, 41, 42)]
    assert cli(*RESPOND, write_items(tmp_path, RESPONSE_ITEMS)) == 0
    assert issue.deleted == ["repos/o/r/git/refs/vergil/ruling-draft/7/41"]
