"""Tests for ``vrg-scope-change``, the human's scope change tool, with GitHub faked."""

from __future__ import annotations

import contextlib
import hashlib
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_scope_change
from vergil_tooling.lib import confirm, github, identity_mode
from vergil_tooling.lib.identity_mode import IdentityMode
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import IssueComment
from vergil_tooling.lib.rulings.marker import MARKER_PREFIX, Marker, render_marker
from vergil_tooling.lib.rulings.scope import is_scope_change
from vergil_tooling.lib.trusted_authors import TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

AGENT = "h-vergil-user"
HUMAN = "h"
AUTHORS = TrustedAuthors(human=(HUMAN,), agents=(AGENT, "h-vergil-audit"))
REQUEST_MARKER = render_marker(Marker("request", None, None))
SCOPE_REF = "refs/vergil/scope-draft/7"
ARGS = ("--issue", "o/r#7")
BODY = "Scope change\n\nAlso cover the docs.\n"


def request_comment(cid: int) -> IssueComment:
    body = f"R1.1: fix or file? Recommendation: fix\n\n{REQUEST_MARKER}"
    return IssueComment(cid, AGENT, body, False)


def reply_comment(cid: int) -> IssueComment:
    return IssueComment(cid, HUMAN, "R1.1: fix", False)


def response_comment(cid: int, request_id: int, reply_id: int) -> IssueComment:
    marker = render_marker(Marker("response", request_id, reply_id))
    return IssueComment(cid, AGENT, f"R1.1: fixed\n\n{marker}", False)


class FakeGh:
    """``o/r#7`` and its scope draft ref: comments, the human's POST, the draft API."""

    def __init__(self) -> None:
        self.comments: list[IssueComment] = [IssueComment(10, AGENT, "findings", False)]
        self.posted: list[str] = []
        self.post_error: Exception | None = None
        self.post_lands = True
        self.read_error_after_post: Exception | None = None
        self.next_id = 100
        self.drafts: dict[str, str] = {}
        self.fetch_draft_calls = 0
        self.delete_error: Exception | None = None
        self.login: str | Exception = HUMAN

    def add_comment(self, *, author: str, body: str = "a comment") -> None:
        self.comments.append(IssueComment(self.next_id + 50, author, body, False))

    def fetch_comments(self, repo: str, issue: int) -> list[IssueComment]:
        assert (repo, issue) == ("o/r", 7)
        if self.posted and self.read_error_after_post is not None:
            raise self.read_error_after_post
        return list(self.comments)

    def fetch_scope_draft(self, repo: str, issue: int) -> str | None:
        assert (repo, issue) == ("o/r", 7)
        self.fetch_draft_calls += 1
        return self.drafts.get(f"refs/vergil/scope-draft/{issue}")

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

    def __call__(self, *argv: str) -> int:
        try:
            code = vrg_scope_change.main(list(argv))
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
    monkeypatch.setattr(
        "vergil_tooling.lib.rulings.draft_ref.fetch_scope_draft", fake.fetch_scope_draft
    )
    monkeypatch.setattr(github, "post_json_once", fake.post_json_once)
    monkeypatch.setattr(github, "delete_if_exists", fake.delete_if_exists)
    monkeypatch.setattr(github, "read_output", fake.read_output)
    monkeypatch.setattr("vergil_tooling.lib.rulings.post.time.sleep", lambda _s: None)
    monkeypatch.setattr(vrg_scope_change, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.HUMAN)
    monkeypatch.setattr(vrg_scope_change, "_is_interactive", lambda: True)
    return fake


@pytest.fixture
def cli(capsys: pytest.CaptureFixture[str], gh: FakeGh, monkeypatch: pytest.MonkeyPatch) -> Cli:
    runner = Cli(capsys, gh)
    monkeypatch.setattr(confirm, "confirm", runner.confirm)
    return runner


@pytest.fixture
def draft(gh: FakeGh) -> Callable[[str], None]:
    def push(text: str) -> None:
        gh.drafts[SCOPE_REF] = text

    return push


def _api_error(stderr: str) -> github.GitHubAPIError:
    return github.GitHubAPIError(1, ("gh", "api"), "", stderr)


# --- who may run it ---------------------------------------------------------


@pytest.mark.parametrize("mode", [IdentityMode.USER, IdentityMode.AUDIT])
def test_refuses_in_agent_mode(
    cli: Cli, gh: FakeGh, monkeypatch: pytest.MonkeyPatch, mode: IdentityMode
) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: mode)
    gh.drafts[SCOPE_REF] = "Also cover the docs.\n"
    assert cli(*ARGS) == 1
    assert "human-only" in cli.stderr
    assert "vrg-ruling draft-scope" in cli.stderr
    assert cli.posted_body is None
    assert cli.prompts == []


def test_agent_help_redirects_before_parsing(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identity_mode, "current_mode", lambda: IdentityMode.USER)
    assert cli("--help") == 0
    assert "human-only command" in cli.stdout
    assert "vrg-ruling draft-scope" in cli.stdout


def test_refuses_wrong_login(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    gh.login = "someone"
    draft("Also cover the docs.\n")
    assert cli(*ARGS) == 1
    assert "other than the human login h" in cli.stderr
    assert "run vrg-scope-change in the human's own shell" in cli.stderr
    assert "someone" not in cli.stderr
    assert gh.fetch_draft_calls == 0
    assert cli.posted_body is None


def test_refuses_the_app_login(cli: Cli, gh: FakeGh) -> None:
    gh.login = AGENT
    assert cli(*ARGS, "--text", "x") == 1
    assert cli.posted_body is None


def test_refuses_when_the_login_cannot_be_read(cli: Cli, gh: FakeGh) -> None:
    gh.login = _api_error("gh: Resource not accessible by integration (HTTP 403)")
    assert cli(*ARGS, "--text", "x") == 1
    assert "cannot read the login" in cli.stderr
    assert cli.posted_body is None


def test_refuses_without_tty(cli: Cli, gh: FakeGh, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vrg_scope_change, "_is_interactive", lambda: False)
    gh.drafts[SCOPE_REF] = "Also cover the docs.\n"
    assert cli(*ARGS) == 1
    assert "interactive terminal" in cli.stderr
    assert cli.posted_body is None


def test_refuses_several_human_logins(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    two = TrustedAuthors(human=(HUMAN, "other"), agents=(AGENT,))
    monkeypatch.setattr(vrg_scope_change, "load_trusted_authors", lambda: two)
    assert cli(*ARGS) == 1
    assert "exactly one human login" in cli.stderr


def test_refuses_when_the_agent_login_is_not_trusted(
    cli: Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = TrustedAuthors(human=(HUMAN,), agents=("x-vergil-user",))
    monkeypatch.setattr(vrg_scope_change, "load_trusted_authors", lambda: other)
    assert cli(*ARGS) == 1
    assert "not on the trusted authors' agents list" in cli.stderr


def test_bad_issue_ref_is_refused(cli: Cli) -> None:
    assert cli("--issue", "o/r") == 1
    assert "owner/repo#N" in cli.stderr


def test_missing_issue_is_a_usage_error(cli: Cli) -> None:
    assert cli() == 2


# --- the turn ------------------------------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        [request_comment(41)],
        [request_comment(41), reply_comment(42)],
    ],
)
def test_refuses_while_an_exchange_is_open(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None], extra: list[IssueComment]
) -> None:
    gh.comments += extra
    draft("Also cover the docs.\n")
    assert cli(*ARGS) == 1
    assert "has open ruling request 41" in cli.stderr
    assert gh.fetch_draft_calls == 0
    assert cli.posted_body is None
    assert cli.prompts == []


def test_posts_after_a_closed_exchange(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    gh.comments += [request_comment(41), reply_comment(42), response_comment(43, 41, 42)]
    draft("Also cover the docs.\n")
    cli.answer("y")
    assert cli(*ARGS) == 0
    assert cli.posted_body == BODY


def test_refuses_without_a_draft(cli: Cli) -> None:
    assert cli(*ARGS) == 1
    assert "no draft scope change for o/r#7" in cli.stderr
    assert "vrg-ruling draft-scope" in cli.stderr
    assert cli.posted_body is None


# --- what is shown is what is posted -----------------------------------------


def test_posts_exactly_what_was_shown(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = OrgSpy()
    monkeypatch.setattr(github, "target_org", spy)
    draft("Also cover the docs.\n")
    cli.answer("y")
    assert cli(*ARGS) == 0
    assert cli.posted_body == BODY
    assert f"exactly as it will be posted -----\n{BODY}----- end of the scope change -----\n" in (
        cli.stdout
    )
    data = BODY.encode()
    assert f"bytes: {len(data)}" in cli.stdout
    assert f"sha256: {hashlib.sha256(data).hexdigest()}" in cli.stdout
    assert cli.stdout.endswith("100\n")
    assert "posted scope change 100 on o/r#7" in cli.stderr
    assert cli.prompts == ["Post this scope change as h?"]
    assert SCOPE_REF not in gh.drafts
    assert spy.orgs == {"o"}
    assert is_scope_change(gh.comments[-1], HUMAN)


def test_repushed_draft_is_not_posted(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    draft("Also cover the docs.\n")
    cli.answer("y", then=lambda gh: gh.drafts.update({SCOPE_REF: "Drop the docs.\n"}))
    assert cli(*ARGS) == 0
    assert cli.posted_body == BODY
    assert gh.fetch_draft_calls == 1


def test_rechecks_after_confirmation(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    draft("Also cover the docs.\n")
    cli.answer("y", then=lambda gh: gh.comments.append(request_comment(41)))
    assert cli(*ARGS) == 1
    assert "changed while the prompt waited" in cli.stderr
    assert cli.posted_body is None
    assert gh.drafts[SCOPE_REF] == "Also cover the docs.\n"


def test_untrusted_comment_while_waiting_does_not_block(
    cli: Cli, draft: Callable[[str], None]
) -> None:
    draft("Also cover the docs.\n")
    cli.answer("y", then=lambda gh: gh.add_comment(author="stranger"))
    assert cli(*ARGS) == 0
    assert cli.posted_body == BODY


def test_declined_keeps_draft(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    draft("Also cover the docs.\n")
    cli.answer("n")
    assert cli(*ARGS) == 1
    assert "declined" in cli.stderr
    assert cli.posted_body is None
    assert gh.drafts[SCOPE_REF] == "Also cover the docs.\n"


def test_text_posts_without_reading_or_deleting_the_draft(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("Drop the docs.\n")
    cli.answer("y")
    assert cli(*ARGS, "--text", "Also cover the docs.") == 0
    assert cli.posted_body == BODY
    assert gh.fetch_draft_calls == 0
    assert gh.drafts[SCOPE_REF] == "Drop the docs.\n"
    assert gh.comments[-1].author == HUMAN


@pytest.mark.parametrize(
    ("bad", "where"),
    [
        ("café", "U+00E9 at offset 3"),
        ("a\tb", "U+0009 at offset 1"),
        ("a‮b", "U+202E at offset 1"),
        ("a\x1b[31mb", "U+001B at offset 1"),
    ],
)
def test_refuses_non_ascii_text(cli: Cli, bad: str, where: str) -> None:
    assert cli(*ARGS, "--text", bad) == 1
    assert f"is not plain text: character {where}" in cli.stderr
    assert cli.posted_body is None
    assert cli.prompts == []


def test_refuses_non_ascii_draft(cli: Cli, draft: Callable[[str], None]) -> None:
    draft("café\n")
    assert cli(*ARGS) == 1
    assert "is not plain text" in cli.stderr
    assert "-----" not in cli.stdout


def test_refuses_a_marker_in_the_text(cli: Cli) -> None:
    assert cli(*ARGS, "--text", f"ok\n{MARKER_PREFIX} kind=request -->") == 1
    assert "marker prefix" in cli.stderr
    assert cli.posted_body is None


@pytest.mark.parametrize("blank", ["", " \n\n"])
def test_refuses_blank_text(cli: Cli, blank: str) -> None:
    assert cli(*ARGS, "--text", blank) == 1
    assert "blank" in cli.stderr
    assert cli.posted_body is None


def test_refuses_text_carrying_the_header(cli: Cli) -> None:
    assert cli(*ARGS, "--text", "Scope change\n\nx") == 1
    assert "header is added for you" in cli.stderr


# --- already posted -----------------------------------------------------------


def test_already_posted_from_the_draft_posts_nothing_and_deletes_it(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    gh.comments.append(IssueComment(55, HUMAN, BODY, False))
    draft("Also cover the docs.\n")
    assert cli(*ARGS) == 0
    assert cli.stdout == "55\n"
    assert "already posted on o/r#7 as comment 55" in cli.stderr
    assert cli.posted_body is None
    assert cli.prompts == []
    assert SCOPE_REF not in gh.drafts


def test_already_posted_by_the_agent_is_not_the_human_scope(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    gh.comments.append(IssueComment(55, AGENT, BODY, False))
    draft("Also cover the docs.\n")
    cli.answer("y")
    assert cli(*ARGS) == 0
    assert cli.posted_body == BODY


def test_already_posted_with_text_keeps_the_draft(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    gh.comments.append(IssueComment(55, HUMAN, BODY, False))
    draft("Drop the docs.\n")
    assert cli(*ARGS, "--text", "Also cover the docs.") == 0
    assert cli.posted_body is None
    assert gh.drafts[SCOPE_REF] == "Drop the docs.\n"


# --- posting failures ---------------------------------------------------------


def test_post_that_did_not_land(cli: Cli, gh: FakeGh, draft: Callable[[str], None]) -> None:
    draft("Also cover the docs.\n")
    gh.post_lands = False
    gh.post_error = _api_error("gh: Bad Gateway (HTTP 502)")
    cli.answer("y")
    assert cli(*ARGS) == 1
    assert "did not land" in cli.stderr
    assert "is safe" in cli.stderr
    assert gh.posted == [BODY]
    assert gh.drafts[SCOPE_REF] == "Also cover the docs.\n"


def test_post_that_landed_despite_an_error(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("Also cover the docs.\n")
    gh.post_error = _api_error("gh: Bad Gateway (HTTP 502)")
    cli.answer("y")
    assert cli(*ARGS) == 0
    assert gh.posted == [BODY]
    assert SCOPE_REF not in gh.drafts


def test_post_outcome_unknown_then_rerun_settles_it(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("Also cover the docs.\n")
    gh.post_error = _api_error("gh: Bad Gateway (HTTP 502)")
    gh.read_error_after_post = RuntimeError("graphql down")
    cli.answer("y")
    assert cli(*ARGS) == 1
    assert "could not tell whether the comment landed" in cli.stderr
    assert "the re-run finds it and posts nothing" in cli.stderr
    # GitHub recovers; the re-run finds the landed comment.
    gh.read_error_after_post = None
    gh.post_error = None
    assert cli(*ARGS) == 0
    assert gh.posted == [BODY]
    assert cli.stdout == "100\n"
    assert SCOPE_REF not in gh.drafts


def test_draft_delete_failure_after_posting(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("Also cover the docs.\n")
    gh.delete_error = _api_error("gh: Server Error (HTTP 500)")
    cli.answer("y")
    assert cli(*ARGS) == 1
    assert "the scope change 100 is posted, but deleting the draft ref" in cli.stderr
    assert "posts nothing and retries the deletion" in cli.stderr
    assert gh.posted == [BODY]
    # The re-run finds the posted comment and retries the deletion.
    gh.delete_error = None
    assert cli(*ARGS) == 0
    assert gh.posted == [BODY]
    assert SCOPE_REF not in gh.drafts


def test_draft_api_error_is_reported(cli: Cli, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> str | None:
        raise RulingError("the draft ref does not hold exactly one file")

    monkeypatch.setattr("vergil_tooling.lib.rulings.draft_ref.fetch_scope_draft", broken)
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


def test_missing_token_on_the_draft_delete_reaches_the_cli_boundary(
    cli: Cli, gh: FakeGh, draft: Callable[[str], None]
) -> None:
    draft("Also cover the docs.\n")
    gh.delete_error = _no_token()
    cli.answer("y")
    assert cli(*ARGS) == 1
    assert "token" in cli.stderr.lower()
    assert "is posted, but deleting" not in cli.stderr
    assert gh.posted == [BODY]
