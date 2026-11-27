"""Tests for ``vergil_tooling.lib.rulings.comments``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

import vergil_tooling.lib.github
from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import trust
from vergil_tooling.lib.rulings import RulingError
from vergil_tooling.lib.rulings.comments import (
    STREAM_ROLES,
    IssueComment,
    Stream,
    parse_issue_ref,
    read_stream,
)
from vergil_tooling.lib.trusted_authors import OrgConfig, TrustedAuthors

if TYPE_CHECKING:
    from tests.vergil_tooling.trust_stubs import FakeItems

KEY = ("o/r", 7)
# The stubs' human and agent, and an automation App configured for the org.
AUTHORS = TrustedAuthors(
    human=("human",), agents=("vergil-user",), orgs={"o": OrgConfig("o-automation")}
)
AUTOMATION: dict[str, str] = {"__typename": "Bot", "login": "o-automation"}
CREATED = trust_stubs.CREATED
EDITED = trust_stubs.EDITED


@pytest.fixture
def items(monkeypatch: pytest.MonkeyPatch) -> FakeItems:
    fake = trust_stubs.FakeItems(strict=True)
    monkeypatch.setattr(vergil_tooling.lib.github, "graphql", fake)
    return fake


def _issue(items: FakeItems, *nodes: dict[str, Any]) -> None:
    items.items[KEY] = trust_stubs.item(nodes=list(nodes))


# --- parse_issue_ref ---------------------------------------------------------


def test_parse_issue_ref_accepts_owner_repo_number() -> None:
    assert parse_issue_ref("vergil-project/vergil-tooling#3923") == (
        "vergil-project/vergil-tooling",
        3923,
    )
    assert parse_issue_ref("o.x/r_y-z#1") == ("o.x/r_y-z", 1)


@pytest.mark.parametrize(
    "ref",
    ["o/r", "#7", "o/r#0", "o/r#07", "o/r#-1", "r#7", "o/r/x#7", "o/r#7 ", "o/r#7\n", "o/r#٧"],
)
def test_parse_issue_ref_refuses_other_forms(ref: str) -> None:
    with pytest.raises(RulingError, match="owner/repo#N"):
        parse_issue_ref(ref)


# --- read_stream ---------------------------------------------------------------


def test_stream_roles_are_the_human_and_the_agents() -> None:
    assert frozenset({trust.Role.HUMAN, trust.Role.AGENTS}) == STREAM_ROLES


def test_read_stream_keeps_trusted_and_reports_every_untrusted_comment(
    items: FakeItems, capsys: pytest.CaptureFixture[str]
) -> None:
    human, agent = trust_stubs.HUMAN, trust_stubs.AGENT
    spam = trust_stubs.SPAM_TEXT
    _issue(
        items,
        trust_stubs.comment(1, "the human", human, editor=human),
        trust_stubs.comment(2, "the agent", agent),
        trust_stubs.comment(3, spam, trust_stubs.OUTSIDER),
        trust_stubs.comment(4, spam, trust_stubs.IMPOSTOR),
        trust_stubs.comment(5, spam, None),
        trust_stubs.comment(6, spam, AUTOMATION),
        trust_stubs.comment(7, spam, agent, editor=trust_stubs.OUTSIDER),
        trust_stubs.comment(8, "the agent again", agent, editor=agent),
    )
    stream = read_stream("o/r", 7, AUTHORS)
    assert stream == Stream(
        comments=[
            IssueComment(1, "human", "the human", True, CREATED),
            IssueComment(2, "vergil-user", "the agent", False, CREATED),
            IssueComment(8, "vergil-user", "the agent again", True, CREATED),
        ],
        all_ids=frozenset(range(1, 9)),
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        f"skipped untrusted content: o/r#7 comment drive-by at {CREATED} on comment 3"
        " (untrusted)\n"
        f"skipped untrusted content: o/r#7 comment vergil-user at {CREATED} on comment 4"
        " (wrong-type)\n"
        f"skipped untrusted content: o/r#7 comment a deleted account at {CREATED} on comment 5"
        " (deleted)\n"
        "ignored content from a trusted role this reader does not accept:"
        f" o/r#7 comment o-automation at {CREATED} on comment 6 (not-accepted)\n"
        f"skipped untrusted content: o/r#7 edit drive-by at {EDITED} on comment 7"
        " (untrusted)\n"
    )
    assert "SPAM-MARKER-TEXT" not in captured.err
    assert "example.invalid" not in captured.err


def test_read_stream_under_actions_reports_counts_only(
    items: FakeItems, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    spam = trust_stubs.SPAM_TEXT
    _issue(
        items,
        trust_stubs.comment(1, spam, trust_stubs.OUTSIDER),
        trust_stubs.comment(2, spam, trust_stubs.IMPOSTOR),
        trust_stubs.comment(3, "kept", trust_stubs.HUMAN),
    )
    assert [c.id for c in read_stream("o/r", 7, AUTHORS).comments] == [3]
    err = capsys.readouterr().err
    assert err == "skipped 2 untrusted item(s) (untrusted: 1, wrong-type: 1)\n"
    assert "drive-by" not in err
    assert "SPAM-MARKER-TEXT" not in err


def test_read_stream_reports_nothing_when_all_are_trusted(
    items: FakeItems, capsys: pytest.CaptureFixture[str]
) -> None:
    _issue(items, trust_stubs.comment(1, "a note"))
    assert read_stream("o/r", 7, AUTHORS).comments == [
        IssueComment(1, "human", "a note", False, CREATED)
    ]
    assert capsys.readouterr().err == ""


def test_read_stream_keeps_64_bit_ids(items: FakeItems) -> None:
    _issue(items, trust_stubs.comment("4300000001", "a note"))
    stream = read_stream("o/r", 7, AUTHORS)
    assert stream.comments[0].id == 4300000001
    assert stream.all_ids == frozenset({4300000001})


def test_read_stream_asks_for_full_database_ids_and_keeps_created_at(
    items: FakeItems, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: list[str] = []

    def spy(query: str, **variables: Any) -> Any:
        queries.append(query)
        return items(query, **variables)

    monkeypatch.setattr(vergil_tooling.lib.github, "graphql", spy)
    _issue(items, trust_stubs.comment(5, "a note"))
    assert read_stream("o/r", 7, AUTHORS).comments[0].created_at == CREATED
    assert queries == [trust._ITEM_QUERY]
    assert "fullDatabaseId" in queries[0]
    assert "databaseId" not in queries[0].replace("fullDatabaseId", "")
    assert "createdAt" in queries[0]
    assert items.reads == [KEY]


def test_read_stream_of_an_issue_with_none(items: FakeItems) -> None:
    _issue(items)
    assert read_stream("o/r", 7, AUTHORS) == Stream(comments=[], all_ids=frozenset())


def test_read_stream_narrowed_to_the_agents_leaves_the_human_out(items: FakeItems) -> None:
    _issue(
        items,
        trust_stubs.comment(1, "the human"),
        trust_stubs.comment(2, "the agent", trust_stubs.AGENT),
    )
    stream = read_stream("o/r", 7, AUTHORS, roles=frozenset({trust.Role.AGENTS}))
    assert [c.id for c in stream.comments] == [2]
    assert stream.all_ids == frozenset({1, 2})


def test_read_stream_loads_the_configured_authors(
    items: FakeItems, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: AUTHORS)
    _issue(items, trust_stubs.comment(1, "a note", trust_stubs.AGENT))
    assert [c.author for c in read_stream("o/r", 7).comments] == ["vergil-user"]


@pytest.mark.parametrize("raw_id", ["0", "012", "1٢"])
def test_read_stream_refuses_an_unusable_comment_id(items: FakeItems, raw_id: str) -> None:
    _issue(items, trust_stubs.comment(raw_id, "a note"))
    with pytest.raises(RulingError, match="o/r#7: a comment has no usable fullDatabaseId"):
        read_stream("o/r", 7, AUTHORS)


@pytest.mark.parametrize(
    "answer",
    [
        {},
        {"repository": None},
        {"repository": {"issueOrPullRequest": None}},
        # An actor without __typename: its type is never guessed.
        trust_stubs.item(nodes=[trust_stubs.comment(1, "x", {"login": "human"})]),
    ],
)
def test_read_stream_raises_when_the_read_is_incomplete(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]
) -> None:
    monkeypatch.setattr(vergil_tooling.lib.github, "graphql", lambda _query, **_v: answer)
    with pytest.raises(trust.TrustReadError, match=r"read_issue o/r#7"):
        read_stream("o/r", 7, AUTHORS)
