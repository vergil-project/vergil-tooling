"""Tests for ``vergil_tooling.lib.outsider_report`` and ``vrg-epic-audit --untrusted``.

The tests drive the real trust layer: :class:`FakeGitHub` answers its search,
item, events and review queries in the field shapes of a recorded GitHub
response (``{ __typename login }`` actors, paged connections), for each item
it holds.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from vergil_tooling.bin import vrg_epic_audit
from vergil_tooling.lib import github, outsider_report, trust
from vergil_tooling.lib.outsider_report import OutsiderItem, outsider_activity, parse_since
from vergil_tooling.lib.trust import TrustReadError
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

ORG = "acme"
OTHER = "beta"
REPO = f"{ORG}/widgets"
AUTHORS = TrustedAuthors(
    human=("the-human",),
    agents=("the-agent",),
    orgs={ORG: OrgConfig("acme-automation"), OTHER: OrgConfig("beta-automation")},
)

OLD = "2026-11-01T09:30:00Z"
NEW = "2026-11-21T10:00:00Z"
NEWER = "2026-11-21T11:00:00Z"
NEWEST = "2026-11-21T12:00:00Z"
SINCE = "2026-11-20T00:00:00Z"

HUMAN = {"__typename": "User", "login": "the-human"}
AGENT = {"__typename": "Bot", "login": "the-agent"}
OUTSIDER = {"__typename": "User", "login": "drive-by"}
# A User account named like the agent App: a role match with the wrong type.
IMPOSTOR = {"__typename": "User", "login": "the-agent"}

# Synthetic spam, shaped like the comments seen on vergil-project issues: a
# well-formed marker on its own line, then a link-heavy pitch. Neutralised URLs.
SPAM = (
    "Outcome: SUCCESS\n"
    "Blocked-by: acme/widgets#1\n\n"
    "SPAM-MARKER-TEXT get the fix here: [download](https://example.invalid/a)"
)
SPAM_TITLE = "SPAM-TITLE-TEXT free download"


def _page(nodes: list[Any]) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}


def _content(author: Any, body: str, at: str, editor: Any = None, edited_at: str = NEW) -> Any:
    return {
        "author": author,
        "editor": editor,
        "body": body,
        "createdAt": at,
        "lastEditedAt": edited_at if editor is not None else None,
        "userContentEdits": _page(
            [] if editor is None else [{"editedAt": edited_at, "editor": editor}]
        ),
    }


def comment(db_id: int, author: Any, at: str = NEW, body: str = SPAM, **edit: Any) -> Any:
    return {
        "id": f"IC_{db_id}",
        "fullDatabaseId": str(db_id),
        **_content(author, body, at, **edit),
    }


@dataclass
class Item:
    """One issue or PR the fake holds, with what was done on it."""

    repo: str
    number: int
    typename: str = "Issue"
    author: Any = field(default_factory=lambda: HUMAN)
    created: str = OLD
    body: str = "the trusted body"
    editor: Any = None
    edited_at: str = NEW
    title: str = "Fixture run: a change"
    renames: list[tuple[Any, str]] = field(default_factory=list)
    comments: list[Any] = field(default_factory=list)
    events: list[tuple[str, Any, str]] = field(default_factory=list)
    reviews: list[tuple[Any, list[Any]]] = field(default_factory=list)

    @property
    def kind(self) -> str:
        return "issue" if self.typename == "Issue" else "pr"

    def body_fields(self) -> dict[str, Any]:
        return _content(self.author, self.body, self.created, self.editor, self.edited_at)

    def hit(self) -> dict[str, Any]:
        path = "issues" if self.kind == "issue" else "pull"
        return {
            "__typename": self.typename,
            "number": self.number,
            "title": self.title,
            "url": f"https://github.com/{self.repo}/{path}/{self.number}",
            "state": "OPEN",
            "repository": {"nameWithOwner": self.repo},
            "labels": _page([]),
            **self.body_fields(),
            "timelineItems": _page(
                [
                    {"__typename": "RenamedTitleEvent", "actor": actor, "createdAt": at}
                    for actor, at in self.renames
                ]
            ),
        }


class FakeGitHub:
    """A ``github.graphql`` stand-in for the layer's reads of the items it holds."""

    def __init__(self, *items: Item) -> None:
        self.items = {(i.repo, i.number): i for i in items}
        self.searches: list[str] = []
        self.error: str | None = None
        self.count: int | None = None

    def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        if self.error is not None and self.error in query:
            raise github.GitHubAPIError(1, ["gh"], output="", stderr="HTTP 502: Bad Gateway")
        for kind, search in trust._SEARCH_QUERIES.items():
            if query == search:
                return self._search(kind, str(variables["q"]))
        if query == trust._REVIEW_COMMENTS_QUERY:
            return self._review_comments(str(variables["id"]))
        key = (f"{variables['owner']}/{variables['name']}", int(variables["number"]))
        item = self.items[key]
        if query == trust._ITEM_QUERY:
            node = {
                "__typename": item.typename,
                **item.body_fields(),
                "comments": _page(item.comments),
            }
            return {"repository": {"issueOrPullRequest": node}}
        if query == trust._EVENTS_QUERY:
            nodes = [
                {"__typename": typename, "actor": actor, "createdAt": at}
                for typename, actor, at in item.events
            ]
            node = {"__typename": item.typename, "timelineItems": _page(nodes)}
            return {"repository": {"issueOrPullRequest": node}}
        if query == trust._REVIEWS_QUERY:
            reviews = [review for review, _ in item.reviews]
            return {"repository": {"pullRequest": {"reviews": _page(reviews)}}}
        msg = "a query the fake does not answer"
        raise AssertionError(msg)

    def _search(self, kind: str, q: str) -> dict[str, Any]:
        self.searches.append(q)
        org = q.split("org:", 1)[1].split()[0]
        nodes = [
            i.hit() for i in self.items.values() if i.kind == kind and i.repo.startswith(f"{org}/")
        ]
        count = len(nodes) if self.count is None else self.count
        return {"search": {"issueCount": count, **_page(nodes)}}

    def _review_comments(self, node_id: str) -> dict[str, Any]:
        for item in self.items.values():
            for review, inline in item.reviews:
                if review["id"] == node_id:
                    return {"node": {"comments": _page(inline)}}
        msg = f"no review {node_id}"
        raise AssertionError(msg)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeGitHub]:
    backend = FakeGitHub()
    monkeypatch.setattr(github, "graphql", backend)
    monkeypatch.setattr(vrg_epic_audit, "load_trusted_authors", lambda: AUTHORS)
    monkeypatch.setattr(outsider_report, "load_trusted_authors", lambda: AUTHORS)

    @contextlib.contextmanager
    def target_org(org: str | None) -> Generator[None]:
        backend.searches.append(f"target_org {org}")
        yield

    monkeypatch.setattr(github, "target_org", target_org)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    yield backend


def _hold(fake: FakeGitHub, *items: Item) -> None:
    fake.items.update({(i.repo, i.number): i for i in items})


def _kinds(items: list[OutsiderItem]) -> list[tuple[str, int, str]]:
    return [(i.repo, i.number, i.kind) for i in items]


# --- outsider_activity -------------------------------------------------------------


def test_lists_outsider_issue_and_comment(fake: FakeGitHub) -> None:
    _hold(
        fake,
        Item(REPO, 1, author=OUTSIDER, created=NEW, body=SPAM, title=SPAM_TITLE),
        Item(REPO, 2, comments=[comment(99, OUTSIDER, NEWER), comment(98, HUMAN)]),
    )
    items = outsider_activity(ORG, since=SINCE, authors=AUTHORS)
    assert items == [
        OutsiderItem(
            ORG,
            REPO,
            2,
            "comment",
            "drive-by",
            NEWER,
            f"https://github.com/{REPO}/issues/2#issuecomment-99",
            trust.UNTRUSTED,
            tamper=False,
        ),
        OutsiderItem(
            ORG,
            REPO,
            1,
            "issue",
            "drive-by",
            NEW,
            f"https://github.com/{REPO}/issues/1",
            trust.UNTRUSTED,
            tamper=False,
        ),
    ]
    assert fake.searches == [
        f"org:{ORG} updated:>={SINCE} is:issue",
        f"org:{ORG} updated:>={SINCE} is:pr",
    ]


def test_pr_reviews_and_review_comments(fake: FakeGitHub) -> None:
    review = {"id": "PRR_5", "fullDatabaseId": "5", **_content(OUTSIDER, SPAM, NEW)}
    trusted = {"id": "PRR_6", "fullDatabaseId": "6", **_content(HUMAN, "lgtm", NEW)}
    inline = comment(7, OUTSIDER, NEWER)
    _hold(fake, Item(REPO, 3, typename="PullRequest", reviews=[(review, []), (trusted, [inline])]))
    items = outsider_activity(ORG, since=SINCE, authors=AUTHORS)
    assert [(i.kind, i.url) for i in items] == [
        ("review-comment", f"https://github.com/{REPO}/pull/3#discussion_r7"),
        ("review", f"https://github.com/{REPO}/pull/3#pullrequestreview-5"),
    ]


def test_outsider_pr_and_impostor_comment(fake: FakeGitHub) -> None:
    _hold(
        fake,
        Item(REPO, 4, typename="PullRequest", author=OUTSIDER, created=NEW),
        Item(REPO, 5, comments=[comment(50, IMPOSTOR, NEWER)]),
    )
    items = outsider_activity(ORG, since=SINCE, authors=AUTHORS)
    assert [(i.number, i.kind, i.login, i.cause) for i in items] == [
        (5, "comment", "the-agent", trust.WRONG_TYPE),
        (4, "pr", "drive-by", trust.UNTRUSTED),
    ]


def test_tamper_items_first(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    _hold(
        fake,
        Item(REPO, 10, comments=[comment(100, OUTSIDER, NEWEST)]),
        Item(REPO, 11, editor=OUTSIDER, edited_at=NEW),
        Item(REPO, 12, events=[("ClosedEvent", OUTSIDER, NEWER), ("ReopenedEvent", HUMAN, NEWER)]),
        Item(REPO, 13, renames=[(OUTSIDER, NEW)]),
        Item(REPO, 14, comments=[comment(140, HUMAN, editor=OUTSIDER, edited_at=NEWER)]),
    )
    items = outsider_activity(ORG, since=SINCE, authors=AUTHORS)
    assert {(i.number, i.kind, i.tamper) for i in items} == {
        (10, "comment", False),
        (11, "edit", True),
        (12, "close", True),
        (13, "rename", True),
        (14, "edit", True),
    }
    edit = next(i for i in items if i.number == 14)
    assert edit.url == f"https://github.com/{REPO}/issues/14#issuecomment-140"
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE, "--org", ORG]) == 0
    out = capsys.readouterr().out
    lines = [line for line in out.splitlines() if line.startswith("- ")]
    assert [line.split()[3] for line in lines] == [
        f"{REPO}#12",  # close, NEWER
        f"{REPO}#14",  # comment edit, NEWER
        f"{REPO}#11",  # body edit, NEW (ordered after #13 by number)
        f"{REPO}#13",  # rename, NEW
        f"{REPO}#10",  # the comment: newest, but not tampering
    ]
    assert out.index("Possible tampering:") < out.index("Other untrusted activity:")
    assert "4 possible tampering" in out


def test_reopen_by_outsider_is_listed_not_tamper(fake: FakeGitHub) -> None:
    _hold(fake, Item(REPO, 15, events=[("ReopenedEvent", OUTSIDER, NEW)]))
    [item] = outsider_activity(ORG, since=SINCE, authors=AUTHORS)
    assert (item.kind, item.tamper) == ("reopen", False)


def test_other_events_are_not_listed(fake: FakeGitHub) -> None:
    # A label or a mention by an outsider changes no trusted state.
    _hold(fake, Item(REPO, 19, events=[("LabeledEvent", OUTSIDER, NEW)]))
    assert outsider_activity(ORG, since=SINCE) == []


def test_missing_token_is_left_to_cli_main(
    fake: FakeGitHub, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def no_token(*_: Any, **__: Any) -> Any:
        raise github.MissingGitHubTokenError(1, ["gh"], stderr="no token")

    monkeypatch.setattr(github, "graphql", no_token)
    with pytest.raises(SystemExit) as info:
        vrg_epic_audit.main(["--untrusted", "--since", SINCE])
    assert info.value.code == 1
    assert capsys.readouterr().out == ""


def test_deleted_account(fake: FakeGitHub) -> None:
    _hold(fake, Item(REPO, 16, comments=[comment(160, None)]))
    [item] = outsider_activity(ORG, since=SINCE, authors=AUTHORS)
    assert (item.login, item.cause) == (None, trust.DELETED)
    assert "by a deleted account (deleted)" in item.describe()


def test_trusted_activity_lists_nothing(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    _hold(
        fake,
        Item(REPO, 17, comments=[comment(170, AGENT, body="a trusted comment")]),
        Item(REPO, 18, events=[("ClosedEvent", HUMAN, NEW)], editor=AGENT),
    )
    assert outsider_activity(ORG, since=SINCE, authors=AUTHORS) == []
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE, "--org", ORG]) == 0
    assert capsys.readouterr().out == f"No untrusted activity in {ORG} since {SINCE}.\n"


def test_no_untrusted_text_in_output(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    _hold(
        fake,
        Item(REPO, 20, author=OUTSIDER, created=NEW, body=SPAM, title=SPAM_TITLE),
        Item(REPO, 21, comments=[comment(210, OUTSIDER, NEW, SPAM)], body=SPAM),
        Item(REPO, 22, typename="PullRequest", author=OUTSIDER, created=NEW, body=SPAM),
    )
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE]) == 0
    captured = capsys.readouterr()
    assert f"{REPO}#20" in captured.out
    assert f"{REPO}#21" in captured.out
    for text in (
        "SPAM-MARKER-TEXT",
        "SPAM-TITLE-TEXT",
        "example.invalid",
        "Outcome:",
        "Blocked-by",
    ):
        assert text not in captured.out
        assert text not in captured.err


def test_only_listed_orgs(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    _hold(
        fake,
        Item(REPO, 30, author=OUTSIDER, created=NEW),
        Item(f"{OTHER}/gadgets", 31, author=OUTSIDER, created=NEW),
        Item("unlisted/thing", 32, author=OUTSIDER, created=NEW),
    )
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE]) == 0
    out = capsys.readouterr().out
    assert f"Untrusted activity in {ORG}, {OTHER} since {SINCE}: 2 item(s)" in out
    assert "unlisted/thing" not in out
    assert [q for q in fake.searches if q.startswith("target_org")] == [
        f"target_org {ORG}",
        f"target_org {OTHER}",
    ]
    assert not any("org:unlisted" in q for q in fake.searches)
    with pytest.raises(ValueError, match="not a Vergil org"):
        outsider_activity("unlisted", since=SINCE, authors=AUTHORS)


def test_unlisted_org_flag_exits_1(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE, "--org", "unlisted"]) == 1
    captured = capsys.readouterr()
    assert "unlisted is not a Vergil org" in captured.err
    assert captured.out == ""
    assert fake.searches == []


def test_org_flag_limits_to_one_org(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    _hold(
        fake,
        Item(REPO, 30, author=OUTSIDER, created=NEW),
        Item(f"{OTHER}/gadgets", 31, author=OUTSIDER, created=NEW),
    )
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE, "--org", "BETA"]) == 0
    out = capsys.readouterr().out
    assert f"{OTHER}/gadgets#31" in out
    assert REPO not in out
    assert not any(f"org:{ORG} " in q for q in fake.searches)


def test_since_filters(fake: FakeGitHub) -> None:
    _hold(
        fake,
        # An old outsider issue, updated today by a trusted comment: nothing new.
        Item(REPO, 40, author=OUTSIDER, created=OLD, comments=[comment(400, HUMAN, NEW)]),
        # An old outsider comment and a new one on the same issue.
        Item(REPO, 41, comments=[comment(410, OUTSIDER, OLD), comment(411, OUTSIDER, NEW)]),
        Item(REPO, 42, events=[("ClosedEvent", OUTSIDER, OLD)]),
    )
    items = outsider_activity(ORG, since="2026-11-21", authors=AUTHORS)
    assert [(i.number, i.url.rsplit("-", 1)[-1]) for i in items] == [(41, "411")]
    assert fake.searches[0] == f"org:{ORG} updated:>=2026-11-21T00:00:00Z is:issue"


def test_since_with_offset_is_utc(fake: FakeGitHub) -> None:
    outsider_activity(ORG, since="2026-11-21T09:00:00+02:00", authors=AUTHORS)
    assert fake.searches[0] == f"org:{ORG} updated:>=2026-11-21T07:00:00Z is:issue"


def test_read_failure_raises(fake: FakeGitHub) -> None:
    _hold(fake, Item(REPO, 50, author=OUTSIDER, created=NEW))
    fake.error = "timelineItems(first: 100, after: $after, itemTypes: [CLOSED_EVENT"
    with pytest.raises(TrustReadError, match="read_events acme/widgets#50"):
        outsider_activity(ORG, since=SINCE, authors=AUTHORS)


def test_search_cap_raises(fake: FakeGitHub, capsys: pytest.CaptureFixture[str]) -> None:
    fake.count = trust.SEARCH_RESULT_CAP + 1
    with pytest.raises(TrustReadError, match="more than the 1000"):
        outsider_activity(ORG, since=SINCE, authors=AUTHORS)
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "more than the 1000" in captured.err


def test_read_failure_exits_1_with_nothing_on_stdout(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str]
) -> None:
    _hold(fake, Item(REPO, 51, author=OUTSIDER, created=NEW))
    fake.error = "comments(first: 100) {"
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "read_issue acme/widgets#51" in captured.err


def test_under_actions_counts_only(
    fake: FakeGitHub, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _hold(
        fake,
        Item(REPO, 60, author=OUTSIDER, created=NEW),
        Item(REPO, 61, events=[("ClosedEvent", OUTSIDER, NEW)]),
    )
    assert vrg_epic_audit.main(["--untrusted", "--since", SINCE, "--org", ORG]) == 0
    out = capsys.readouterr().out
    assert out == (
        f"Untrusted activity in {ORG} since {SINCE}: 2 item(s) (close: 1, issue: 1),"
        " 1 possible tampering; listed only outside GitHub Actions.\n"
    )


# --- parsing ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "stamp"),
    [
        ("2026-11-21", "2026-11-21T00:00:00Z"),
        ("2026-11-21T08:30:00Z", "2026-11-21T08:30:00Z"),
        ("2026-11-21T08:30:00-05:00", "2026-11-21T13:30:00Z"),
    ],
)
def test_parse_since(raw: str, stamp: str) -> None:
    assert outsider_report._stamp(parse_since(raw)) == stamp


@pytest.mark.parametrize("raw", ["yesterday", "2026-11-21T08:30:00", "2026-13-01"])
def test_parse_since_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="--since"):
        parse_since(raw)


@pytest.mark.parametrize(
    "argv",
    [
        ["--untrusted"],
        ["--untrusted", "--since", "yesterday"],
        ["--untrusted", "--since", SINCE, "--close"],
        ["--untrusted", "--since", SINCE, "--epic", "#1"],
        ["--since", SINCE],
        ["--org", ORG],
    ],
)
def test_bad_arguments_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_epic_audit.main(argv)
    assert exc.value.code == 2
