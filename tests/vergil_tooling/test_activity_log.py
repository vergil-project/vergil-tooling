"""Tests for vergil_tooling.lib.activity_log."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import activity_log, trust
from vergil_tooling.lib.activity_log import ActivityItem

TOOLING = "vergil-project/vergil-tooling"
_SEARCH = trust._CLOSE_SEARCH_QUERIES["issue"]


def _page(nodes: list[Any], cursor: str | None = None) -> dict[str, Any]:
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}


def _closed(
    number: int,
    title: str = "labels",
    *,
    repo: str = TOOLING,
    author: Any = trust_stubs.HUMAN,
    closer: Any = trust_stubs.HUMAN,
    closed_at: str = "2026-06-29T10:00:00Z",
    body: str = "real work",
) -> dict[str, Any]:
    """A closed issue as the layer's close-judging search returns it."""
    return {
        "__typename": "Issue",
        "number": number,
        "title": title,
        "url": f"u{number}",
        "state": "CLOSED",
        "repository": {"nameWithOwner": repo},
        "labels": _page([]),
        "author": author,
        "editor": None,
        "body": body,
        "createdAt": trust_stubs.CREATED,
        "lastEditedAt": None,
        "userContentEdits": _page([]),
        "timelineItems": _page([]),
        "closes": {
            "nodes": [{"__typename": "ClosedEvent", "actor": closer, "createdAt": closed_at}]
        },
    }


class FakeSearch:
    """A ``github.graphql`` stand-in answering the close-judging search, page by page."""

    def __init__(self, *pages: tuple[list[dict[str, Any]], str | None, int]) -> None:
        self.pages = list(pages)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, query: str, **variables: Any) -> dict[str, Any]:
        assert query == _SEARCH, "only the close-judging issue search is faked"
        self.calls.append(variables)
        nodes, cursor, count = self.pages.pop(0)
        return {"search": {"issueCount": count, **_page(nodes, cursor)}}


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)

    def install(*nodes: dict[str, Any]) -> FakeSearch:
        fake = FakeSearch((list(nodes), None, len(nodes)))
        monkeypatch.setattr("vergil_tooling.lib.github.graphql", fake)
        return fake

    return install


def test_gather_reads_closed_issues_through_the_trust_layer(search: Any) -> None:
    fake = search(_closed(1912))
    items = activity_log.gather("2026-06-01", org="vergil-project")
    assert items == [ActivityItem(TOOLING, 1912, "labels", "u1912", "2026-06-29")]
    # the search carries the org scope and the since cutoff
    assert [call["q"] for call in fake.calls] == [
        "user:vergil-project is:closed is:issue closed:>=2026-06-01"
    ]


def test_gather_dates_an_issue_by_its_latest_close(search: Any) -> None:
    search(_closed(7, closed_at="2026-06-30T23:59:00Z"))
    assert [i.closed_date for i in activity_log.gather("2026-06-01", org="o")] == ["2026-06-30"]


def test_activity_log_omits_outsider_issue(search: Any, capsys: pytest.CaptureFixture[str]) -> None:
    search(
        _closed(1, "kept"),
        _closed(2, trust_stubs.SPAM_TEXT, author=trust_stubs.OUTSIDER, body=trust_stubs.SPAM_TEXT),
        _closed(3, "closed by an outsider", closer=trust_stubs.OUTSIDER),
        _closed(4, "closed by an impostor", closer=trust_stubs.IMPOSTOR),
        _closed(5, "opened by a deleted account", author=None),
        _closed(6, "closed by the agent", closer=trust_stubs.AGENT),
    )
    items = activity_log.gather("2026-06-01", org="vergil-project")
    assert [item.number for item in items] == [1, 6]
    err = capsys.readouterr().err
    assert f"{TOOLING}#2 issue drive-by" in err and "on body (untrusted)" in err
    assert f"{TOOLING}#3 event drive-by" in err and "on close (untrusted)" in err
    assert f"{TOOLING}#4 event vergil-user" in err and "on close (wrong-type)" in err
    assert f"{TOOLING}#5 issue a deleted account" in err
    assert "SPAM-MARKER-TEXT" not in err


def test_activity_log_skips_are_counts_only_under_actions(
    search: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    search(_closed(1), _closed(2, author=trust_stubs.OUTSIDER), _closed(3, closer=None))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    items = activity_log.gather("2026-06-01", org="vergil-project")
    assert [item.number for item in items] == [1]
    err = capsys.readouterr().err
    assert err == "skipped 2 untrusted item(s) (deleted: 1, untrusted: 1)\n"


def test_gather_skips_release_tracking_issues(search: Any) -> None:
    # A closed ``release: X.Y.Z`` issue is vrg-release bookkeeping, not work —
    # keep it out of the ledger, whether detected by body marker or by title.
    search(
        _closed(1912),
        _closed(
            373,
            "release: 2.1.4",
            repo="vergil-project/vergil-containers",
            body="<!-- vrg-release:progress -->\n- [x] tag\n",
        ),
        _closed(400, "release: 2.1.5", repo="vergil-project/vergil-containers", body=""),
    )
    items = activity_log.gather("2026-06-01", org="vergil-project")
    assert items == [ActivityItem(TOOLING, 1912, "labels", "u1912", "2026-06-29")]


def test_gather_defaults_org_to_current_repo_owner(search: Any) -> None:
    fake = search()
    with patch("vergil_tooling.lib.github.current_org", return_value="acme") as mock_org:
        assert activity_log.gather("2026-06-01") == []
    mock_org.assert_called_once()
    assert fake.calls[0]["q"].startswith("user:acme ")


def test_gather_repo_scopes_search_to_one_repo(search: Any) -> None:
    fake = search()
    activity_log.gather("2026-06-01", org="vergil-project", repo="vergil-tooling")
    assert fake.calls[0]["q"].startswith(f"repo:{TOOLING} is:closed ")
    assert "user:" not in fake.calls[0]["q"]


def test_gather_repo_accepts_owner_name(search: Any) -> None:
    fake = search()
    activity_log.gather("2026-06-01", org="vergil-project", repo="acme/widgets")
    assert fake.calls[0]["q"].startswith("repo:acme/widgets ")


def test_gather_graphql_calls_bounded_by_result_pages(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A window over the search cap is halved once; then 1,150 results take 12
    # pages. 13 calls in all: one per page and one per halving, never per issue.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    issues = [_closed(n) for n in range(1, 1151)]
    first, second = issues[:600], issues[600:]
    pages: list[tuple[list[dict[str, Any]], str | None, int]] = [([], "S0", 1150)]
    for half in (first, second):
        chunks = [half[i : i + 100] for i in range(0, len(half), 100)]
        pages += [
            (chunk, f"P{i}" if i < len(chunks) - 1 else None, len(half))
            for i, chunk in enumerate(chunks)
        ]
    fake = FakeSearch(*pages)
    monkeypatch.setattr("vergil_tooling.lib.github.graphql", fake)
    items = activity_log.gather("2026-06-01", org="vergil-project")
    assert len(items) == 1150
    assert len(fake.calls) == 13
    assert capsys.readouterr().err == ""


def test_gather_one_day_over_the_cap_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    fake = FakeSearch(([], "S0", 1001))
    monkeypatch.setattr("vergil_tooling.lib.github.graphql", fake)
    with (
        patch.object(trust, "datetime") as clock,
        pytest.raises(trust.TrustReadError, match="more than the 1000"),
    ):
        clock.now.return_value.date.return_value = trust.date(2026, 6, 1)
        activity_log.gather("2026-06-01", org="vergil-project")


def test_gather_never_guesses_a_missing_close_time(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = trust.TrustedHit(TOOLING, 1, "issue", "t", "u1", "CLOSED", (), "", trust.Role.HUMAN)
    monkeypatch.setattr(trust, "search_window", lambda *a, **k: ([hit], []))
    with pytest.raises(trust.TrustReadError, match="u1 has no close time"):
        activity_log.gather("2026-06-01", org="vergil-project")


def test_gather_read_failure_raises(search: Any) -> None:
    broken = _closed(1)
    broken["closes"] = {"nodes": []}
    search(broken)
    with pytest.raises(trust.TrustReadError, match="has no close event"):
        activity_log.gather("2026-06-01", org="vergil-project")


def test_render_empty() -> None:
    assert "No recently closed issues" in activity_log.render([])


def test_render_groups_by_date_descending() -> None:
    items = [
        ActivityItem("vergil-project/vergil-tooling", 1912, "labels", "u1912", "2026-06-28"),
        ActivityItem("vergil-project/vergil-tooling", 1926, "umbrella", "u1926", "2026-06-29"),
        ActivityItem("vergil-project/.github", 41, "docs", "u41", "2026-06-29"),
    ]
    out = activity_log.render(items)
    assert "3 issue(s) closed" in out
    # most recent date first
    assert out.index("## 2026-06-29") < out.index("## 2026-06-28")
    assert "[vergil-project/.github#41](u41) docs" in out
    assert "[vergil-project/vergil-tooling#1912](u1912) labels" in out


def test_render_takes_a_title() -> None:
    items = [ActivityItem("vergil-project/vergil-tooling", 1, "t", "u1", "2026-06-28")]
    assert activity_log.render(items, title="Activity log: vergil-tooling").startswith(
        "# Activity log: vergil-tooling\n"
    )
    assert activity_log.render([], title="X").startswith("# X\n")


def test_repo_page_names() -> None:
    assert activity_log.repo_page("vergil-project/vergil-tooling") == "vergil-tooling.md"
    # MkDocs skips dot-files, so .github must not render as .github.md
    assert activity_log.repo_page("vergil-project/.github") == "dot-github.md"


_ITEMS = [
    ActivityItem("vergil-project/vergil-tooling", 1912, "labels", "u1912", "2026-06-28"),
    ActivityItem("vergil-project/vergil-tooling", 1926, "umbrella", "u1926", "2026-06-29"),
    ActivityItem("vergil-project/vergil-tooling", 1920, "other", "u1920", "2026-06-29"),
    ActivityItem("vergil-project/.github", 41, "docs", "u41", "2026-06-29"),
]


def test_render_summary_empty() -> None:
    assert "No recently closed issues" in activity_log.render_summary([])


def test_render_summary_table() -> None:
    out = activity_log.render_summary(_ITEMS)
    assert "4 issue(s) closed" in out
    rows = [line for line in out.splitlines() if line.startswith("|")]
    assert rows == [
        "| Day | Total | [.github](repos/dot-github.md)"
        " | [vergil-tooling](repos/vergil-tooling.md) |",
        "| --- | ---: | ---: | ---: |",
        "| [2026-06-29](days/2026-06-29.md) | 3 | 1 | 2 |",
        "| [2026-06-28](days/2026-06-28.md) | 1 |  | 1 |",
    ]


def test_render_day_groups_by_repo() -> None:
    day = [item for item in _ITEMS if item.closed_date == "2026-06-29"]
    out = activity_log.render_day("2026-06-29", day)
    assert out.startswith("# 2026-06-29\n")
    assert "3 issue(s) closed" in out
    assert "](../index.md)" in out
    assert out.index("## vergil-project/.github") < out.index("## vergil-project/vergil-tooling")
    # within a repo, issues are in number order
    assert out.index("#1920]") < out.index("#1926]")
    assert out.index("## vergil-project/vergil-tooling") < out.index("#1920]")


def test_render_pages_layout() -> None:
    pages = activity_log.render_pages(_ITEMS)
    assert sorted(pages) == [
        "days/2026-06-28.md",
        "days/2026-06-29.md",
        "index.md",
        "repos/dot-github.md",
        "repos/vergil-tooling.md",
    ]
    assert pages["index.md"] == activity_log.render_summary(_ITEMS)
    repo = pages["repos/vergil-tooling.md"]
    assert repo.startswith("# Activity log: vergil-tooling\n")
    assert "3 issue(s) closed" in repo
    assert "vergil-project/.github" not in repo


def test_render_pages_empty() -> None:
    assert list(activity_log.render_pages([])) == ["index.md"]


# Hostile titles: tag-like, script-like, link-like and a pipe.
_HOSTILE_TITLES = (
    ("<b>bold</b>", "&lt;b&gt;bold&lt;/b&gt;"),
    ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
    ("[x](javascript:alert(1))", "\\[x\\](javascript:alert(1))"),
    ("a|b", "a\\|b"),
)


def test_activity_line_escapes_title() -> None:
    for title, escaped in _HOSTILE_TITLES:
        item = ActivityItem("vergil-project/vergil-tooling", 7, title, "u7", "2026-06-28")
        line = activity_log._line(item)
        assert line == f"- [vergil-project/vergil-tooling#7](u7) {escaped}"
        assert "<" not in line
        # The pipe is escaped exactly once.
        assert "\\\\|" not in line
        # Every page that lists the item carries the escaped title.
        for page in activity_log.render_pages([item]).values():
            assert "<" not in page
