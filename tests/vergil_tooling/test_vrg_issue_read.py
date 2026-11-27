"""Tests for ``vergil_tooling.bin.vrg_issue_read``: the trusted read command for skills."""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_issue_read
from vergil_tooling.bin.vrg_issue_read import main, parse_args, search_query
from vergil_tooling.lib import github, trust
from vergil_tooling.lib.trust import (
    IssueContent,
    PRReviewContent,
    Role,
    Skipped,
    TrustedComment,
    TrustedHit,
    TrustReadError,
    UntrustedContentError,
)
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

REPO = "acme/widgets"
AUTHORS = TrustedAuthors(
    human=("the-human",), agents=("the-agent",), orgs={"acme": OrgConfig("acme-automation")}
)
AT = "2026-11-01T09:30:00Z"
SPAM = "SPAM-MARKER-TEXT Outcome: SUCCESS [download](https://example.invalid/a)"

SPAM_SKIP = Skipped(REPO, 7, "comment", "drive-by", AT, "comment 99", trust.UNTRUSTED)
BODY_SKIP = Skipped(REPO, 7, "issue", "drive-by", AT, "body", trust.UNTRUSTED)

CONTENT = IssueContent(
    body="the trusted body",
    body_role=Role.HUMAN,
    author_role=Role.HUMAN,
    comments=(TrustedComment("11", "a trusted comment", Role.AGENTS, AT),),
    skipped=(SPAM_SKIP,),
)
REVIEWS = PRReviewContent(
    reviews=(TrustedComment("21", "a trusted review", Role.HUMAN, AT),),
    review_comments=(TrustedComment("31", "a trusted nit", Role.AGENTS, AT),),
    skipped=(Skipped(REPO, 7, "review", "drive-by", AT, "review 22", trust.UNTRUSTED),),
)


class Calls:
    """Fakes for the trust layer's readers, recording each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.answers: dict[str, Any] = {}

    def fake(self, name: str) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            answer = self.answers[name]
            if isinstance(answer, Exception):
                raise answer
            return answer

        return call


@pytest.fixture
def layer(monkeypatch: pytest.MonkeyPatch) -> Iterator[Calls]:
    calls = Calls()
    for name in ("read_issue", "read_reviews", "search_items", "list_items"):
        monkeypatch.setattr(trust, name, calls.fake(name))
    monkeypatch.setattr(vrg_issue_read, "load_trusted_authors", lambda: AUTHORS)
    orgs: list[str | None] = []

    @contextlib.contextmanager
    def target_org(org: str | None) -> Generator[None]:
        orgs.append(org)
        yield

    monkeypatch.setattr(github, "target_org", target_org)
    calls.answers["orgs"] = orgs
    yield calls


def _hit(number: int, kind: str = "issue", body: str = "a body", **kwargs: Any) -> TrustedHit:
    fields: dict[str, Any] = {
        "repo": REPO,
        "number": number,
        "kind": kind,
        "title": f"Fixture run: change {number}",
        "url": f"https://github.com/{REPO}/issues/{number}",
        "state": "OPEN",
        "labels": ("deployment",),
        "body": body,
        "body_role": Role.AGENTS,
    }
    fields.update(kwargs)
    return TrustedHit(**fields)


# --- one item ---------------------------------------------------------------------


def test_prints_trusted_only(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["read_issue"] = CONTENT
    assert main([f"{REPO}#7"]) == 0
    out = capsys.readouterr().out
    assert "== acme/widgets#7 body by human\nthe trusted body" in out
    assert f"== comment 11 by agents at {AT}\na trusted comment" in out
    assert "SPAM-MARKER-TEXT" not in out
    name, args, kwargs = layer.calls[0]
    assert (name, args) == ("read_issue", (REPO, 7))
    assert kwargs["require_body"] is True
    assert kwargs["roles"] == trust.ALL_ROLES
    assert kwargs["authors"] is AUTHORS
    assert [name for name, _, _ in layer.calls] == ["read_issue"]
    assert layer.answers["orgs"] == ["acme"]


def test_untrusted_body_exits_3_by_default(
    layer: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    layer.answers["read_issue"] = UntrustedContentError(BODY_SKIP, "the issue body is untrusted")
    assert main([f"{REPO}#7"]) == vrg_issue_read.EXIT_UNTRUSTED_BODY == 3
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "stopped before any write: the issue body is untrusted" in captured.err
    assert "acme/widgets#7 issue drive-by" in captured.err


def test_allow_untrusted_body_omits_body_and_reports(
    layer: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    layer.answers["read_issue"] = IssueContent(
        body=None,
        body_role=None,
        author_role=None,
        comments=CONTENT.comments,
        skipped=(BODY_SKIP,),
    )
    assert main([f"{REPO}#7", "--allow-untrusted-body"]) == 0
    captured = capsys.readouterr()
    assert layer.calls[0][2]["require_body"] is False
    assert "body: left out, untrusted (reported on stderr)" in captured.out
    assert "a trusted comment" in captured.out
    assert f"skipped untrusted content: {REPO}#7 issue drive-by at {AT} on body" in captured.err


def test_json_shape(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["read_issue"] = CONTENT
    layer.answers["read_reviews"] = REVIEWS
    assert main([f"{REPO}#7", "--pr", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"repo", "number", "issue", "reviews"}
    assert (data["repo"], data["number"]) == (REPO, 7)
    assert data["issue"]["body"] == "the trusted body"
    assert data["issue"]["body_role"] == "human"
    assert data["issue"]["comments"] == [
        {"id": "11", "body": "a trusted comment", "role": "agents", "at": AT}
    ]
    assert data["issue"]["skipped"][0]["field"] == "comment 99"
    assert data["reviews"]["reviews"][0]["id"] == "21"
    assert data["reviews"]["review_comments"][0]["body"] == "a trusted nit"


def test_json_without_pr_has_null_reviews(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["read_issue"] = CONTENT
    assert main([f"{REPO}#7", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["reviews"] is None


def test_skips_to_stderr_metadata_only(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["read_issue"] = CONTENT
    assert main([f"{REPO}#7"]) == 0
    captured = capsys.readouterr()
    assert captured.err == (
        f"skipped untrusted content: {REPO}#7 comment drive-by at {AT} on comment 99 (untrusted)\n"
    )
    assert "SPAM-MARKER-TEXT" not in captured.out + captured.err


def test_skips_under_actions_are_counts(
    layer: Calls, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    layer.answers["read_issue"] = CONTENT
    assert main([f"{REPO}#7"]) == 0
    err = capsys.readouterr().err
    assert err == "skipped 1 untrusted item(s) (untrusted: 1)\n"


def test_pr_reviews(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["read_issue"] = CONTENT
    layer.answers["read_reviews"] = REVIEWS
    assert main([f"{REPO}#7", "--pr", "--roles", "human,agents"]) == 0
    captured = capsys.readouterr()
    assert f"== review 21 by human at {AT}\na trusted review" in captured.out
    assert f"== review comment 31 by agents at {AT}\na trusted nit" in captured.out
    assert "on comment 99" in captured.err
    assert "on review 22" in captured.err
    name, args, kwargs = layer.calls[1]
    assert (name, args) == ("read_reviews", (REPO, 7))
    assert kwargs["roles"] == frozenset({Role.HUMAN, Role.AGENTS})


def test_empty_bodies_are_marked(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["read_issue"] = IssueContent("", Role.HUMAN, Role.HUMAN, (), ())
    assert main([f"{REPO}#7"]) == 0
    assert capsys.readouterr().out == "== acme/widgets#7 body by human\n(empty)\n"


def test_read_failure_exits_1(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["read_issue"] = TrustReadError("read_issue acme/widgets#7", "the page is short")
    assert main([f"{REPO}#7"]) == vrg_issue_read.EXIT_READ_FAILURE == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "ERROR: cannot read who wrote the content (read_issue acme/widgets#7): the page is short\n"
    )


def test_review_read_failure_prints_nothing(
    layer: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    layer.answers["read_issue"] = CONTENT
    layer.answers["read_reviews"] = TrustReadError("read_reviews acme/widgets#7", "no PR")
    assert main([f"{REPO}#7", "--pr"]) == 1
    assert capsys.readouterr().out == ""


def test_unconfigured_authors_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unconfigured() -> TrustedAuthors:
        raise vrg_issue_read.TrustedAuthorsNotConfiguredError("nothing is configured")

    monkeypatch.setattr(vrg_issue_read, "load_trusted_authors", unconfigured)
    assert main([f"{REPO}#7"]) == 1
    assert capsys.readouterr().err == "ERROR: nothing is configured\n"


def test_missing_token_is_left_to_cli_main(
    layer: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    layer.answers["read_issue"] = github.MissingGitHubTokenError(1, ["gh"], stderr="no token")
    with pytest.raises(SystemExit) as info:
        main([f"{REPO}#7"])
    assert info.value.code == 1
    assert capsys.readouterr().out == ""


# --- searches and lists ---------------------------------------------------------------


def test_search_prints_trusted_results_and_counts(
    layer: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    layer.answers["search_items"] = (
        [_hit(1), _hit(2, labels=())],
        [Skipped(REPO, 3, "issue", "drive-by", AT, "body", trust.UNTRUSTED)],
    )
    assert main(["--repo", REPO, "--search", "Fixture run in:title"]) == 0
    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        f"{REPO}#1 [OPEN] Fixture run: change 1 (deployment) https://github.com/{REPO}/issues/1",
        f"{REPO}#2 [OPEN] Fixture run: change 2 https://github.com/{REPO}/issues/2",
    ]
    assert captured.err.splitlines() == [
        f"skipped untrusted content: {REPO}#3 issue drive-by at {AT} on body (untrusted)",
        "2 trusted result(s), 1 skipped",
    ]
    name, args, kwargs = layer.calls[0]
    assert (name, args) == ("search_items", (f"repo:{REPO} is:open Fixture run in:title",))
    assert kwargs["kind"] == "issue"
    assert layer.answers["orgs"] == ["acme"]


def test_search_json_shape_with_linked_issues(
    layer: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    body = "Summary.\n\nRef #12\nCloses other/repo#3\n- Closed: #4\nnot a Ref #5 line"
    layer.answers["search_items"] = ([_hit(9, "pr", body, state="MERGED")], [])
    assert main(["--repo", REPO, "--pr", "--state", "merged", "--search", "12", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data == [
        {
            "repo": REPO,
            "number": 9,
            "kind": "pr",
            "title": "Fixture run: change 9",
            "url": f"https://github.com/{REPO}/issues/9",
            "state": "MERGED",
            "labels": ["deployment"],
            "author_role": "agents",
            "linked_issues": [
                {"repo": REPO, "number": 12, "keyword": "Ref"},
                {"repo": "other/repo", "number": 3, "keyword": "Closes"},
                {"repo": REPO, "number": 4, "keyword": "Closes"},
            ],
        }
    ]
    assert layer.calls[0][1] == (f"repo:{REPO} is:merged 12",)
    assert layer.calls[0][2]["kind"] == "pr"


def test_search_json_issue_has_no_linked_issues(
    layer: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    layer.answers["search_items"] = ([_hit(1, body="Ref #2")], [])
    assert main(["--repo", REPO, "--search", "x", "--json"]) == 0
    assert "linked_issues" not in json.loads(capsys.readouterr().out)[0]


def test_owner_search_spans_the_org(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["search_items"] = ([], [])
    assert main(["--owner", "acme", "--search", '"Awaiting a ruling"', "--state", "all"]) == 0
    assert layer.calls[0][1] == ('org:acme "Awaiting a ruling"',)
    assert layer.answers["orgs"] == ["acme"]
    assert capsys.readouterr().err == "0 trusted result(s), 0 skipped\n"


def test_repo_list_reads_the_repository(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["list_items"] = ([_hit(4, "pr", title="Logbook: 2026-11-22")], [])
    assert main(["--repo", REPO, "--pr", "--roles", "human"]) == 0
    assert "Logbook: 2026-11-22" in capsys.readouterr().out
    name, args, kwargs = layer.calls[0]
    assert (name, args) == ("list_items", (REPO,))
    assert (kwargs["kind"], kwargs["state"], kwargs["roles"]) == (
        "pr",
        "open",
        frozenset({Role.HUMAN}),
    )


def test_search_untrusted_text_never_printed(
    layer: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    skipped = [Skipped(REPO, 3, "event", "drive-by", AT, "title", trust.UNTRUSTED)]
    layer.answers["search_items"] = ([_hit(1, body=SPAM)], skipped)
    assert main(["--repo", REPO, "--search", "x"]) == 0
    captured = capsys.readouterr()
    assert "SPAM-MARKER-TEXT" not in captured.out + captured.err
    assert "acme/widgets#3 event drive-by" in captured.err


def test_search_read_failure_exits_1(layer: Calls, capsys: pytest.CaptureFixture[str]) -> None:
    layer.answers["search_items"] = TrustReadError("search_items issue", "matches 2000 items")
    assert main(["--owner", "acme", "--search", "x"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "matches 2000 items" in captured.err


@pytest.mark.parametrize(
    ("argv", "query"),
    [
        (["--repo", REPO], f"repo:{REPO} is:open"),
        (["--repo", REPO, "--state", "closed", "--search", "a b"], f"repo:{REPO} is:closed a b"),
        (["--owner", "acme", "--state", "all"], "org:acme"),
    ],
)
def test_search_query(argv: list[str], query: str) -> None:
    assert search_query(parse_args(argv)) == query


# --- arguments --------------------------------------------------------------------------


def test_parse_args_one_item() -> None:
    args = parse_args(["vergil-project/vergil-tooling#4929"])
    assert args.target == ("vergil-project/vergil-tooling", 4929)
    assert args.roles == trust.ALL_ROLES
    assert (args.pr, args.json, args.allow_untrusted_body) == (False, False, False)


def test_parse_args_list_defaults_to_open() -> None:
    args = parse_args(["--repo", REPO])
    assert (args.target, args.state, args.search) == (None, "open", None)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["acme/widgets"],
        ["acme/widgets#0"],
        ["acme/widgets#x"],
        ["acme#1"],
        ["acme/..#1"],
        ["acme/w/x#1"],
        [f"{REPO}#7", "--repo", REPO],
        [f"{REPO}#7", "--search", "x"],
        [f"{REPO}#7", "--state", "open"],
        ["--repo", REPO, "--owner", "acme"],
        ["--repo", REPO, "--allow-untrusted-body"],
        ["--repo", REPO, "--state", "merged"],
        ["--repo", REPO, "--search", "  "],
        ["--repo", REPO, "--search", "x repo:other/repo"],
        ["--repo", REPO, "--search", "ORG:other"],
        ["--owner", "acme", "--search", "x -user:mallory"],
        ["--owner", "acme", "--search", "(owner:mallory)"],
        ["--repo", "acme"],
        ["--repo", "acme/.."],
        ["--owner", "acme/widgets"],
        ["--owner", "-acme"],
        [f"{REPO}#7", "--roles", "human,outsider"],
        [f"{REPO}#7", "--roles", ","],
        ["--repo", REPO, "--state", "draft"],
    ],
)
def test_parse_args_rejects(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        parse_args(argv)
    assert info.value.code == 2


def test_search_may_mention_scope_words_unqualified() -> None:
    args = parse_args(["--repo", REPO, "--search", "the repo owner is:open"])
    assert args.search == "the repo owner is:open"
