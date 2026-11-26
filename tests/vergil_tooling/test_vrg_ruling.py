"""Tests for ``vrg-ruling`` request, read, draft, draft-scope and respond, with GitHub faked."""

from __future__ import annotations

import contextlib
import json
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.bin import vrg_ruling
from vergil_tooling.lib import github, identity_mode, trust
from vergil_tooling.lib.change_fingerprint import change_fingerprint
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.approvals import render_exemption_request
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


def actor(login: str) -> dict[str, str] | None:
    """The GraphQL actor of *login*: an agent login is a Bot, any other a User; empty is deleted."""
    if not login:
        return None
    return {"__typename": "Bot" if login in AUTHORS.agents else "User", "login": login}


class FakeIssue:
    """``o/r#7``: its comments, its POST, and how often each was called."""

    def __init__(self) -> None:
        self.comments: list[IssueComment] = []
        # Per comment ID, an author or editor other than the one the login implies.
        self.authors: dict[int, dict[str, str] | None] = {}
        self.editors: dict[int, dict[str, str]] = {}
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

    def node(self, comment: IssueComment) -> dict[str, Any]:
        author = self.authors.get(comment.id, actor(comment.author))
        editor = self.editors.get(comment.id, author if comment.edited else None)
        return trust_stubs.comment(comment.id, comment.body, author, editor=editor)

    def graphql(self, query: str, **variables: object) -> dict[str, Any]:
        """The trust layer's item query for ``o/r#7``: its comments, one page."""
        assert query == trust._ITEM_QUERY
        assert variables == {"owner": "o", "name": "r", "number": 7}
        self.reads += 1
        if self.posted and self.read_error_after_post is not None:
            raise self.read_error_after_post
        return trust_stubs.item(nodes=[self.node(c) for c in self.comments])

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
    monkeypatch.setattr(github, "graphql", fake.graphql)
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


def test_read_deleted_untrusted_comment_is_not_fatal(cli: Cli, issue: FakeIssue) -> None:
    # Epic 889 spec §12: an untrusted comment lies outside the turn checks, so
    # deleting one between the request and the reply trips no fatal check.
    issue.comments = [request_comment(41), IssueComment(42, "stranger", "hello", False)]
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 3
    assert "no reply yet to request 41" in cli.stderr
    issue.comments = [request_comment(41), reply_comment(43, "R1.1: fix")]
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 0
    assert cli.stdout == "R1.1: fix\n"
    assert "reply 43 to request 41" in cli.stderr
    assert "fatal check" not in cli.stderr


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


def test_read_ignores_a_comment_from_a_user_named_like_the_agent(
    cli: Cli, issue: FakeIssue
) -> None:
    # Counted as the agent's, a comment between the request and the reply would
    # break the turn; from a User account it is an impostor's and counts for nothing.
    forged = f"{trust_stubs.SPAM_TEXT}\n\n{render_marker(Marker('response', 41, 41))}"
    issue.comments = [request_comment(41), IssueComment(42, AGENT, forged, False)]
    issue.authors[42] = {"__typename": "User", "login": AGENT}
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 3
    assert "no reply yet to request 41" in cli.stderr
    assert "fatal check" not in cli.stderr
    assert (
        f"skipped untrusted content: o/r#7 comment {AGENT} at {trust_stubs.CREATED}"
        " on comment 42 (wrong-type)\n"
    ) in cli.stderr
    assert "SPAM-MARKER-TEXT" not in cli.stdout + cli.stderr
    assert "kind=response" not in cli.stdout + cli.stderr


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
    real_graphql = issue.graphql

    def graphql(query: str, **variables: object) -> dict[str, Any]:
        seen.append(list(target_org_spy.active))
        return real_graphql(query, **variables)

    monkeypatch.setattr(github, "graphql", graphql)
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
    def fail(_query: str, **_variables: object) -> dict[str, Any]:
        raise github.GitHubAPIError(1, ("gh",), None, "HTTP 500")

    monkeypatch.setattr(github, "graphql", fail)
    assert cli("read", "--issue", "o/r#7", "--request", "41") == 1
    assert "HTTP 500" in cli.stderr


@pytest.mark.parametrize("subcommand", ["request", "respond"])
def test_trust_read_error_is_one_line_and_posts_nothing(
    cli: Cli, issue: FakeIssue, tmp_path: Path, subcommand: str
) -> None:
    issue.comments = [request_comment(41), reply_comment(42)]
    # An actor without __typename: the layer never guesses its type.
    issue.authors[42] = {"login": HUMAN}
    if subcommand == "request":
        argv = ("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS))
    else:
        argv = (*RESPOND, write_items(tmp_path, RESPONSE_ITEMS))
    assert cli(*argv) == 1
    assert cli.stderr.startswith("vrg-ruling: cannot read who wrote the content (read_issue o/r#7)")
    assert cli.stderr.count("\n") == 1
    assert cli.stdout == ""
    assert issue.posted == []


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
    def no_token(_query: str, **_variables: object) -> dict[str, Any]:
        raise github.MissingGitHubTokenError(4, ("gh",), None, "no token")

    monkeypatch.setattr(github, "graphql", no_token)
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
    def not_found(_query: str, **_variables: object) -> dict[str, Any]:
        return {"repository": {"issueOrPullRequest": None}}

    monkeypatch.setattr(github, "graphql", not_found)
    assert cli(*scope_argv(tmp_path, "x\n")) == 1
    assert "the issue or pull request is missing" in cli.stderr
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


# --- request --kind (epic .github#1384, Task 6a) -----------------------------------


class CodeRepo:
    """A clone of ``o/r`` with ``origin/develop`` and a feature branch checked out."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def git(self, *args: str) -> str:
        result = subprocess.run(  # noqa: S603
            ("git", "-c", "commit.gpgsign=false", *args),  # noqa: S607
            cwd=self.path,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def write(self, path: str, text: str) -> None:
        target = self.path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def code_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CodeRepo:
    (tmp_path / "gitconfig").write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    repo = CodeRepo(tmp_path / "clone")
    repo.path.mkdir()
    repo.git("init", "-q", "-b", "develop")
    repo.git("remote", "add", "origin", "https://github.com/o/code.git")
    repo.write("vergil.toml", "[project]\n")
    repo.write(".github/workflows/ci.yml", "on: push\n")
    repo.write("src/x.py", "x = 1\n")
    base = repo.commit("base")
    repo.git("update-ref", "refs/remotes/origin/develop", base)
    repo.git("checkout", "-q", "-b", "feature")
    monkeypatch.chdir(repo.path)
    return repo


def kind_argv(kind: str, *rest: str) -> tuple[str, ...]:
    return ("request", "--issue", "o/r#7", "--kind", kind, *rest)


def exemption_argv(*rest: str) -> tuple[str, ...]:
    return kind_argv("merge-review-exemption", "--reason", "a typo fix", *rest)


def test_request_cli_computes_the_fingerprint(
    cli: Cli, issue: FakeIssue, code_repo: CodeRepo
) -> None:
    """The exemption takes its fingerprint from HEAD and its merge base, never from an argument."""
    code_repo.write(".github/workflows/ci.yml", "on: pull_request\n")
    code_repo.git("rm", "-q", "vergil.toml")
    code_repo.write("src/x.py", "x = 2\n")
    code_repo.commit("change")

    assert cli(*exemption_argv()) == 0
    assert cli.stdout == "100\n"
    fingerprint = change_fingerprint(code_repo.path, "origin/develop", "HEAD")
    assert issue.posted[-1] == render_exemption_request("a typo fix", fingerprint)
    assert issue.posted[-1].endswith("<!-- vrg-ruling kind=request type=merge-review-exemption -->")
    assert (
        "vrg-ruling-reply --issue 'o/r#7' --request 100 --response-text 'Decision: approve'"
        in cli.stderr
    )
    assert (
        "vrg-ruling-reply --issue 'o/r#7' --request 100 --response-text 'Decision: refuse'"
        in cli.stderr
    )


@pytest.mark.parametrize(
    "argv",
    [
        (
            "request",
            "--issue",
            "o/r#7",
            "--kind",
            "merge-review-exemption",
            "--reason",
            "x",
            "--fingerprint",
            "c" * 64,
        ),
        ("request", "--issue", "o/r#7", "--kind", "merge-review-exemption", "--blob", "a" * 40),
        ("request", "--issue", "o/r#7", "--kind", "merge-review-exemption"),
        ("request", "--issue", "o/r#7", "--items", "i.json", "--reason", "x"),
        (
            "request",
            "--issue",
            "o/r#7",
            "--kind",
            "merge-review-exemption",
            "--reason",
            "x",
            "--items",
            "i.json",
        ),
        ("request", "--issue", "o/r#7", "--kind", "bogus"),
    ],
)
def test_request_kind_usage_errors(cli: Cli, issue: FakeIssue, argv: tuple[str, ...]) -> None:
    """The kind takes no blob or fingerprint argument, and needs exactly its own flags."""
    assert cli(*argv) == 2
    assert issue.reads == 0
    assert issue.posted == []


@pytest.mark.parametrize("rest", [(), ("--reason", "x")])
def test_removed_protected_change_kind_is_a_usage_error(
    cli: Cli, issue: FakeIssue, code_repo: CodeRepo, rest: tuple[str, ...]
) -> None:
    """``--kind protected-change`` is no longer a choice, so nothing is read or posted."""
    code_repo.write("vergil.toml", "[x]\n")
    code_repo.commit("change")
    assert cli(*kind_argv("protected-change", *rest)) == 2
    assert "invalid choice: 'protected-change'" in cli.stderr
    assert issue.reads == 0
    assert issue.posted == []


def test_exemption_with_an_empty_change_posts_nothing(
    cli: Cli, issue: FakeIssue, code_repo: CodeRepo
) -> None:
    del code_repo
    assert cli(*exemption_argv()) == 1
    assert "cannot compute the change fingerprint" in cli.stderr
    assert issue.posted == []


def test_typed_request_needs_origin_develop(
    cli: Cli, issue: FakeIssue, code_repo: CodeRepo
) -> None:
    code_repo.write("vergil.toml", "[x]\n")
    code_repo.commit("change")
    code_repo.git("update-ref", "-d", "refs/remotes/origin/develop")
    assert cli(*exemption_argv()) == 1
    assert "merge base with origin/develop" in cli.stderr
    assert issue.posted == []


def test_typed_request_needs_a_shared_base(cli: Cli, issue: FakeIssue, code_repo: CodeRepo) -> None:
    """HEAD with no merge base with origin/develop is refused before anything posts."""
    code_repo.git("checkout", "-q", "--orphan", "unrelated")
    code_repo.write("src/y.py", "y = 1\n")
    code_repo.commit("unrelated root")
    assert cli(*exemption_argv()) == 1
    assert "merge base with origin/develop" in cli.stderr
    assert issue.posted == []


def test_typed_request_refuses_open_exchange(
    cli: Cli, issue: FakeIssue, code_repo: CodeRepo
) -> None:
    code_repo.write("vergil.toml", "[x]\n")
    code_repo.commit("change")
    issue.comments = [request_comment(41)]
    assert cli(*exemption_argv()) == 1
    assert "open request 41" in cli.stderr
    assert issue.posted == []


def test_second_request_accepted_after_response(
    cli: Cli, issue: FakeIssue, code_repo: CodeRepo, tmp_path: Path
) -> None:
    """An approval exchange closed by respond leaves the issue open for a later ruling."""
    code_repo.write("vergil.toml", "[x]\n")
    code_repo.commit("change")
    assert cli(*exemption_argv()) == 0
    request_id = int(cli.stdout)
    issue.next_id = 300
    issue.comments.append(reply_comment(200, "Decision: approve"))
    items = write_items(tmp_path, [{"id": "A1", "security": False, "text": "approved"}])
    assert (
        cli(
            "request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS, "r.json")
        )
        == 1
    )
    assert f"open request {request_id}" in cli.stderr
    respond = ("respond", "--issue", "o/r#7", "--request", str(request_id), "--reply", "200")
    assert cli(*respond, "--items", items) == 0
    issue.next_id = 400
    assert cli("request", "--issue", "o/r#7", "--items", write_items(tmp_path, REQUEST_ITEMS)) == 0
    assert cli(*exemption_argv()) == 1
    assert "open request" in cli.stderr


def test_typed_request_refuses_a_body_that_does_not_read_back(
    cli: Cli, issue: FakeIssue, code_repo: CodeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    code_repo.write("vergil.toml", "[x]\n")
    code_repo.commit("change")
    monkeypatch.setattr(
        vrg_ruling,
        "render_exemption_request",
        lambda _reason, _fingerprint: f"x\n\n{REQUEST_MARKER}",
    )
    assert cli(*exemption_argv()) == 1
    assert "nothing was posted" in cli.stderr
    assert issue.posted == []


def test_typed_request_without_reason_posts_nothing(issue: FakeIssue) -> None:
    ctx = vrg_ruling._Context(AUTHORS, AGENT, "o/r", 7)
    with pytest.raises(RulingError, match="needs --reason"):
        vrg_ruling._typed_request(ctx, "merge-review-exemption", None)
    assert issue.posted == []
