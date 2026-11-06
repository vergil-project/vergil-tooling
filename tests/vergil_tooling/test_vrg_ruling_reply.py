"""Tests for ``vrg-ruling-reply``, the human's reply tool, with GitHub faked."""

from __future__ import annotations

import contextlib
import hashlib
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_ruling_reply
from vergil_tooling.lib import confirm, github, identity_mode
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.marker import MARKER_PREFIX, Marker, render_marker
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

AGENT = "h-vergil-user"
HUMAN = "h"
AUTHORS = TrustedAuthors(human=(HUMAN,), agents=(AGENT, "h-vergil-audit"))
REQUEST_MARKER = render_marker(Marker("request", None, None))
DRAFT_REF = "refs/vergil/ruling-draft/7/41"
ARGS = ("--issue", "o/r#7", "--request", "41")


def request_comment(cid: int, *, edited: bool = False) -> IssueComment:
    body = f"R1.1: fix or file? Recommendation: fix\n\n{REQUEST_MARKER}"
    return IssueComment(cid, AGENT, body, edited)


def reply_comment(cid: int, body: str = "R1.1: fix") -> IssueComment:
    return IssueComment(cid, HUMAN, body, False)


def response_comment(cid: int, request_id: int, reply_id: int) -> IssueComment:
    marker = render_marker(Marker("response", request_id, reply_id))
    return IssueComment(cid, AGENT, f"R1.1: fixed\n\n{marker}", False)


class FakeGh:
    """``o/r#7`` and its draft refs: comments, the human's POST, the draft API."""

    def __init__(self) -> None:
        self.comments: list[IssueComment] = [request_comment(41)]
        self.posted: list[str] = []
        self.post_error: Exception | None = None
        self.post_lands = True
        self.read_error_after_post: Exception | None = None
        self.next_id = 100
        self.drafts: dict[str, str] = {}
        self.fetch_draft_calls = 0
        self.delete_error: Exception | None = None
        self.login: str | Exception = HUMAN

    def ref(self, name: str, *, blob_text: str) -> None:
        self.drafts[f"refs/{name}"] = blob_text

    def add_comment(self, *, author: str, body: str = "a comment") -> None:
        self.comments.append(IssueComment(self.next_id + 50, author, body, False))

    def fetch_comments(self, repo: str, issue: int) -> list[IssueComment]:
        assert (repo, issue) == ("o/r", 7)
        if self.posted and self.read_error_after_post is not None:
            raise self.read_error_after_post
        return list(self.comments)

    def fetch_draft(self, repo: str, issue: int, request_id: int) -> str | None:
        assert (repo, issue) == ("o/r", 7)
        self.fetch_draft_calls += 1
        return self.drafts.get(f"refs/vergil/ruling-draft/{issue}/{request_id}")

    def delete_if_exists(self, endpoint: str) -> bool:
        if self.delete_error is not None:
            raise self.delete_error
        return self.drafts.pop(endpoint.removeprefix("repos/o/r/git/"), None) is not None

    def post_json_once(
        self, endpoint: str, body: dict[str, object], *, timeout: float | None = None
    ) -> dict[str, Any]:
        assert endpoint == "repos/o/r/issues/7/comments"
        assert timeout == 30
        text = str(body["body"])
        self.posted.append(text)
        if self.post_lands:
            self.comments.append(IssueComment(self.next_id, HUMAN, text, False))
        if self.post_error is not None:
            raise self.post_error
        return {"id": self.next_id}

    def read_output(self, *args: str) -> str:
        assert args == ("api", "user", "--jq", ".login")
        if isinstance(self.login, Exception):
            raise self.login
        return self.login


class Cli:
    def __init__(self, capsys: pytest.CaptureFixture[str], gh: FakeGh) -> None:
        self._capsys = capsys
        self._gh = gh
        self.stdout = ""
        self.stderr = ""
        self.prompts: list[str] = []
        self._answer: str | None = None
        self._then: Callable[[FakeGh], None] | None = None

    def answer(self, reply: str, then: Callable[[FakeGh], None] | None = None) -> None:
        """Answer the confirmation prompt; ``then`` runs while the prompt waits."""
        self._answer = reply
        self._then = then

    def confirm(self, prompt: str, *, assume_yes: bool, default: bool = False) -> bool:
        assert assume_yes is False
        assert default is False
        self.prompts.append(prompt)
        assert self._answer is not None, "the tool asked for confirmation unexpectedly"
        if self._then is not None:
            self._then(self._gh)
        return self._answer == "y"

    @property
    def posted_body(self) -> str | None:
        return self._gh.posted[-1] if self._gh.posted else None

    @property
    def fetch_draft_calls(self) -> int:
        return self._gh.fetch_draft_calls

    def __call__(self, *argv: str) -> int:
        try:
            code = vrg_ruling_reply.main(list(argv))
        except SystemExit as exc:
            code = int(exc.code or 0)
        captured = self._capsys.readouterr()
        self.stdout, self.stderr = captured.out, captured.err
        return code


class OrgSpy:
    def __init__(self) -> None:
        self.orgs: set[str | None] = set()

    @contextlib.contextmanager
    def __call__(self, org: str | None) -> Generator[None]:
        self.orgs.add(org)
        yield


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> FakeGh:
    fake = FakeGh()
    monkeypatch.setattr("vergil_tooling.lib.rulings.comments.fetch_comments", fake.fetch_comments)
    monkeypatch.setattr("vergil_tooling.lib.rulings.draft_ref.fetch_draft", fake.fetch_draft)
    monkeypatch.setattr(github, "post_json_once", fake.post_json_once)
    monkeypatch.setattr(github, "delete_if_exists", fake.delete_if_exists)
    monkeypatch.setattr(github, "read_output", fake.read_output)
    monkeypatch.setattr("vergil_tooling.lib.rulings.post.time.sleep", lambda _s: None)
    monkeypatch.setattr(vrg_ruling_reply, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.HUMAN)
    monkeypatch.setattr(vrg_ruling_reply, "_is_interactive", lambda: True)
    return fake


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], gh: FakeGh, monkeypatch: pytest.MonkeyPatch) -> Cli:
    runner = Cli(capsys, gh)
    monkeypatch.setattr(confirm, "confirm", runner.confirm)
    return runner


@pytest.fixture
def draft(gh: FakeGh) -> Callable[[str], None]:
    def push(text: str) -> None:
        gh.drafts[DRAFT_REF] = text

    return push


def _api_error(stderr: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "api"), "", stderr)


# --- who may run it ---------------------------------------------------------


@pytest.mark.parametrize("mode", [IdentityMode.USER, IdentityMode.AUDIT])
def test_refuses_in_agent_mode(
    cli: Cli, gh: FakeGh, monkeypatch: pytest.MonkeyPatch, mode: IdentityMode
) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: mode)
    gh.drafts[DRAFT_REF] = "R1.1: fix\n"
    assert cli(*ARGS) == 1
    assert "human-only" in cli.stderr
    assert cli.posted_body is None
    assert cli.prompts == []


def test_agent_help_redirects_before_parsing(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.USER)
    assert cli("--help") == 0
    assert "human-only command" in cli.stdout
    assert "vrg-ruling draft" in cli.stdout


def test_refuses_wrong_login(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    gh.login = "someone"
    draft("R1.1: fix\n")
    assert cli(*ARGS) == 1
    assert "other than the human login h" in cli.stderr
    assert "someone" not in cli.stderr
    assert gh.fetch_draft_calls == 0
    assert cli.posted_body is None


def test_refuses_when_the_login_cannot_be_read(cli: Cli, gh: FakeGh) -> None:
    gh.login = _api_error("gh: Resource not accessible by integration (HTTP 403)")
    assert cli(*ARGS) == 1
    assert "cannot read the login" in cli.stderr
    assert cli.posted_body is None


def test_refuses_the_app_login(cli: Cli, gh: FakeGh) -> None:
    gh.login = AGENT
    assert cli(*ARGS) == 1
    assert cli.posted_body is None


def test_refuses_without_tty(cli: Cli, gh: FakeGh, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vrg_ruling_reply, "_is_interactive", lambda: False)
    gh.drafts[DRAFT_REF] = "R1.1: fix\n"
    assert cli(*ARGS) == 1
    assert "interactive terminal" in cli.stderr
    assert cli.posted_body is None


class _Stream:
    def __init__(self, *, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.mark.parametrize(
    ("stdin", "stdout", "expected"),
    [(True, True, True), (False, True, False), (True, False, False), (False, False, False)],
)
def test_is_interactive_needs_both_terminals(
    monkeypatch: pytest.MonkeyPatch, *, stdin: bool, stdout: bool, expected: bool
) -> None:
    monkeypatch.setattr("sys.stdin", _Stream(tty=stdin))
    monkeypatch.setattr("sys.stdout", _Stream(tty=stdout))
    assert vrg_ruling_reply._is_interactive() is expected


def test_refuses_several_human_logins(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    two = TrustedAuthors(human=(HUMAN, "other"), agents=(AGENT,))
    monkeypatch.setattr(vrg_ruling_reply, "load_trusted_authors", lambda: two)
    assert cli(*ARGS) == 1
    assert "exactly one human login" in cli.stderr


def test_refuses_when_the_agent_login_is_not_trusted(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = TrustedAuthors(human=(HUMAN,), agents=("x-vergil-user",))
    monkeypatch.setattr(vrg_ruling_reply, "load_trusted_authors", lambda: other)
    assert cli(*ARGS) == 1
    assert "not on the trusted authors' agents list" in cli.stderr


def test_bad_request_id_is_a_usage_error_without_echo(cli: Cli) -> None:
    assert cli("--issue", "o/r#7", "--request", "0x41") == 2
    assert "0x41" not in cli.stderr


def test_bad_issue_ref_is_refused(cli: Cli) -> None:
    assert cli("--issue", "o/r", "--request", "41") == 1
    assert "owner/repo#N" in cli.stderr


# --- the turn checks before anything is shown --------------------------------


def test_refuses_when_request_not_latest(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    gh.comments += [reply_comment(42), response_comment(43, 41, 42)]
    draft("R1.1: fix\n")
    assert cli(*ARGS) == 1
    assert "not the open request" in cli.stderr
    assert gh.fetch_draft_calls == 0
    assert cli.posted_body is None


def test_refuses_when_a_later_request_is_open(cli: Cli, gh: FakeGh) -> None:
    gh.comments += [reply_comment(42), response_comment(43, 41, 42), request_comment(44)]
    assert cli(*ARGS) == 1
    assert "not the open request" in cli.stderr


def test_refuses_when_the_request_already_has_a_reply(cli: Cli, gh: FakeGh) -> None:
    gh.comments.append(reply_comment(42))
    gh.drafts[DRAFT_REF] = "R1.1: fix\n"
    assert cli(*ARGS) == 1
    assert "already has reply 42" in cli.stderr
    assert cli.posted_body is None


def test_edited_request_is_fatal(cli: Cli, gh: FakeGh) -> None:
    gh.comments = [request_comment(41, edited=True)]
    gh.drafts[DRAFT_REF] = "R1.1: fix\n"
    assert cli(*ARGS) == 4
    assert "fatal check 'edited' on comment 41" in cli.stderr
    assert cli.posted_body is None


def test_trusted_comment_out_of_turn_is_fatal(cli: Cli, gh: FakeGh) -> None:
    gh.add_comment(author=AGENT)
    assert cli(*ARGS) == 4
    assert "fatal check 'turn'" in cli.stderr


def test_refuses_without_a_draft(cli: Cli) -> None:
    assert cli(*ARGS) == 1
    assert "no draft reply for request 41" in cli.stderr
    assert cli.posted_body is None


# --- what is shown is what is posted -----------------------------------------


def test_posts_exactly_what_was_shown(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = OrgSpy()
    monkeypatch.setattr(github, "target_org", spy)
    draft("R1.1: fix\n")
    cli.answer("y")
    assert cli(*ARGS) == 0
    assert cli.posted_body == "R1.1: fix\n"
    assert "\nR1.1: fix\n----- end of the reply -----\n" in cli.stdout
    assert "bytes: 10" in cli.stdout
    digest = hashlib.sha256(b"R1.1: fix\n").hexdigest()
    assert f"sha256: {digest}" in cli.stdout
    assert "posted reply 100 to request 41" in cli.stdout
    assert cli.prompts == ["Post this reply as h?"]
    assert gh.fetch_draft("o/r", 7, 41) is None
    assert spy.orgs == {"o"}


def test_repushed_draft_is_not_posted(cli: Cli, draft: Callable[[str], None]) -> None:
    draft("R1.1: fix\n")
    # the draft ref is pushed again while the prompt waits
    cli.answer(
        "y",
        then=lambda gh: gh.ref("vergil/ruling-draft/7/41", blob_text="R1.1: file\n"),
    )
    assert cli(*ARGS) == 0
    assert cli.posted_body == "R1.1: fix\n"
    assert cli.fetch_draft_calls == 1


def test_rechecks_after_confirmation(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    draft("R1.1: fix\n")
    # a comment lands on the issue while the prompt waits
    cli.answer("y", then=lambda gh: gh.add_comment(author="h-vergil-user"))
    assert cli(*ARGS) == 1
    assert "changed while the prompt waited" in cli.stderr
    assert cli.posted_body is None
    assert gh.drafts[DRAFT_REF] == "R1.1: fix\n"


def test_reply_posted_elsewhere_while_waiting_is_refused(
    cli: Cli, draft: Callable[[str], None]
) -> None:
    draft("R1.1: fix\n")
    cli.answer("y", then=lambda gh: gh.comments.append(reply_comment(42)))
    assert cli(*ARGS) == 1
    assert "changed while the prompt waited" in cli.stderr
    assert cli.posted_body is None


def test_untrusted_comment_while_waiting_does_not_block(
    cli: Cli, draft: Callable[[str], None]
) -> None:
    draft("R1.1: fix\n")
    cli.answer("y", then=lambda gh: gh.add_comment(author="stranger"))
    assert cli(*ARGS) == 0
    assert cli.posted_body == "R1.1: fix\n"


def test_declined_keeps_draft(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    draft("R1.1: fix\n")
    cli.answer("n")
    assert cli(*ARGS) == 1
    assert "declined" in cli.stderr
    assert cli.posted_body is None
    assert gh.fetch_draft("o/r", 7, 41) == "R1.1: fix\n"


@pytest.mark.parametrize(
    ("bad", "where"),
    [
        ("aㅤb", "U+3164 at offset 1"),
        ("café", "U+00E9 at offset 3"),
        ("a\tb", "U+0009 at offset 1"),
        ("a‮b", "U+202E at offset 1"),
        ("a​b", "U+200B at offset 1"),
        ("a—b", "U+2014 at offset 1"),
        ("a\x1b[31mb", "U+001B at offset 1"),
    ],
)
def test_refuses_non_ascii_reply(cli: Cli, bad: str, where: str) -> None:
    # from --response-text; nothing is shown, asked or posted
    assert cli(*ARGS, "--response-text", bad) == 1
    assert f"is not plain text: character {where}" in cli.stderr
    assert cli.posted_body is None
    assert cli.prompts == []


@pytest.mark.parametrize("bad", ["aㅤb\n", "café\n", "a\tb\n", "a\rb\n"])
def test_refuses_non_ascii_draft(cli: Cli, draft: Callable[[str], None], bad: str) -> None:
    draft(bad)
    assert cli(*ARGS) == 1
    assert "is not plain text" in cli.stderr
    assert cli.posted_body is None
    assert cli.prompts == []
    assert "-----" not in cli.stdout


def test_refuses_a_marker_in_the_reply(cli: Cli) -> None:
    assert cli(*ARGS, "--response-text", f"ok\n{MARKER_PREFIX} kind=request -->") == 1
    assert "marker prefix" in cli.stderr
    assert cli.posted_body is None


@pytest.mark.parametrize("blank", ["", " \n\n"])
def test_refuses_a_blank_reply(cli: Cli, blank: str) -> None:
    assert cli(*ARGS, "--response-text", blank) == 1
    assert "blank" in cli.stderr
    assert cli.posted_body is None


def test_response_text_also_deletes_draft(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("x\n")
    cli.answer("y")
    assert cli(*ARGS, "--response-text", "deny") == 0
    assert cli.posted_body == "deny"
    assert gh.fetch_draft_calls == 0
    assert "(it has no final newline)" in cli.stdout
    assert "bytes: 4" in cli.stdout
    assert gh.fetch_draft("o/r", 7, 41) is None


def test_response_text_without_a_draft(cli: Cli, gh: FakeGh) -> None:
    cli.answer("y")
    assert cli(*ARGS, "--response-text", "R1.1: wont-fix\n") == 0
    assert cli.posted_body == "R1.1: wont-fix\n"
    assert gh.comments[-1].author == HUMAN


# --- posting failures ---------------------------------------------------------


def test_post_that_did_not_land(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    draft("R1.1: fix\n")
    gh.post_lands = False
    gh.post_error = _api_error("gh: Bad Gateway (HTTP 502)")
    cli.answer("y")
    assert cli(*ARGS) == 1
    assert "did not land" in cli.stderr
    assert "is safe" in cli.stderr
    assert gh.posted == ["R1.1: fix\n"]
    assert gh.drafts[DRAFT_REF] == "R1.1: fix\n"


def test_post_that_landed_despite_an_error(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("R1.1: fix\n")
    gh.post_error = _api_error("gh: Bad Gateway (HTTP 502)")
    cli.answer("y")
    assert cli(*ARGS) == 0
    assert gh.posted == ["R1.1: fix\n"]
    assert "posted reply 100" in cli.stdout
    assert DRAFT_REF not in gh.drafts


def test_post_outcome_unknown(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    draft("R1.1: fix\n")
    gh.post_error = _api_error("gh: Bad Gateway (HTTP 502)")
    gh.read_error_after_post = RuntimeError("graphql down")
    cli.answer("y")
    assert cli(*ARGS) == 1
    assert "could not tell whether the comment landed" in cli.stderr
    assert "already answered" in cli.stderr
    assert gh.posted == ["R1.1: fix\n"]


def test_draft_delete_failure_after_posting(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("R1.1: fix\n")
    gh.delete_error = _api_error("gh: Server Error (HTTP 500)")
    cli.answer("y")
    assert cli(*ARGS) == 1
    assert "the reply 100 is posted, but deleting the draft ref" in cli.stderr
    assert "vrg-ruling respond deletes the leftover ref" in cli.stderr
    assert gh.posted == ["R1.1: fix\n"]


def test_draft_api_error_is_reported(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> str | None:
        raise RulingError("the draft ref does not hold exactly one file")

    monkeypatch.setattr("vergil_tooling.lib.rulings.draft_ref.fetch_draft", broken)
    assert cli(*ARGS) == 1
    assert "does not hold exactly one file" in cli.stderr


def test_github_error_while_reading_is_an_error(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def api_broken(_repo: str, _issue: int) -> list[IssueComment]:
        raise _api_error("gh: Server Error (HTTP 500)")

    monkeypatch.setattr("vergil_tooling.lib.rulings.comments.fetch_comments", api_broken)
    assert cli(*ARGS) == 1
    assert "HTTP 500" in cli.stderr


def _no_token() -> github.MissingGitHubTokenError:
    return github.MissingGitHubTokenError(4, ("gh",), None, "no token")


def test_missing_token_on_the_login_read_reaches_the_cli_boundary(cli: Cli, gh: FakeGh) -> None:
    gh.login = _no_token()
    assert cli(*ARGS) == 1
    assert "token" in cli.stderr.lower()
    assert "cannot read the login" not in cli.stderr


def test_missing_token_on_the_issue_read_reaches_the_cli_boundary(
    cli: Cli, gh: FakeGh, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_token(_repo: str, _issue: int) -> list[IssueComment]:
        raise _no_token()

    monkeypatch.setattr("vergil_tooling.lib.rulings.comments.fetch_comments", no_token)
    assert cli(*ARGS) == 1
    assert "token" in cli.stderr.lower()
    assert gh.posted == []


def test_missing_token_on_the_draft_delete_reaches_the_cli_boundary(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("R1.1: fix\n")
    gh.delete_error = _no_token()
    cli.answer("y")
    assert cli(*ARGS) == 1
    assert "token" in cli.stderr.lower()
    assert "is posted, but deleting" not in cli.stderr
    assert gh.posted == ["R1.1: fix\n"]
