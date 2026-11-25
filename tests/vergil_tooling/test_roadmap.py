"""Tests for vergil_tooling.lib.roadmap."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from tests.vergil_tooling import trust_stubs
from tests.vergil_tooling.subissue_fake import FakeSubIssueGraph
from vergil_tooling.lib import epic_chain, epic_docs, epics, roadmap, trust
from vergil_tooling.lib.roadmap import CompletedEpic, EpicSummary

if TYPE_CHECKING:
    from collections.abc import Callable


def _child(owner: str, repo: str, number: int, state: str) -> epics.ChildState:
    return epics.ChildState(epics.IssueRef(owner, repo, number), state)


def test_gather_summarizes_open_epics_and_skips_adhoc() -> None:
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": {"title": "v2.2"},
            "labels": [{"name": "epic"}],
            "url": "https://github.com/vergil-project/.github/issues/40",
        },
        {
            "number": 5,
            "title": "Ad-hoc",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "ad-hoc"}],  # ad-hoc -> skipped
            "url": "u5",
        },
    ]
    children = [
        _child("vergil-project", "vergil-tooling", 1912, "CLOSED"),
        _child("vergil-project", "vergil-claude-plugin", 524, "OPEN"),
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=children),
    ):
        result = roadmap.gather("vergil-project")
    assert len(result) == 1
    summary = result[0]
    assert summary.number == 40
    assert summary.created == "2026-06-28"
    assert summary.milestone == "v2.2"
    assert summary.repos == (
        "vergil-project/vergil-claude-plugin",
        "vergil-project/vergil-tooling",
    )
    assert (summary.total, summary.closed) == (2, 1)


def test_gather_skips_adhoc_epic() -> None:
    # The ad-hoc bucket is perpetual and excluded from the strategic roadmap.
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u40",
        },
        {
            "number": 6,
            "title": "Epic (ad hoc): vergil-tooling",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "ad-hoc"}],  # ad-hoc -> skipped
            "url": "u6",
        },
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        result = roadmap.gather("vergil-project")
    assert [s.number for s in result] == [40]


def test_roadmap_skips_intake_and_planning_epics() -> None:
    # Intake and planning are perpetual standing queues, not strategic epics.
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u40",
        },
        {
            "number": 7,
            "title": "Epic (intake): vergil-project",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "intake"}],
            "url": "u7",
        },
        {
            "number": 8,
            "title": "Epic (planning): vergil-project",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "planning"}],
            "url": "u8",
        },
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        result = roadmap.gather("vergil-project")
    assert [s.number for s in result] == [40]


def test_gather_handles_no_milestone_and_no_children() -> None:
    epic_list = [
        {
            "number": 7,
            "title": "X",
            "createdAt": "2026-02-02T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u7",
        }
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        result = roadmap.gather("vergil-project")
    assert result[0].milestone is None
    assert result[0].repos == ()


def test_gather_skips_release_tracking_issue() -> None:
    # Defense in depth: a release tracking issue is never epic-labelled, but if a
    # stray ``epic`` label ever lands on one it must not leak into the roadmap.
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u40",
        },
        {
            "number": 373,
            "title": "release: 2.1.4",
            "createdAt": "2026-06-30T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u373",
            "body": "<!-- vrg-release:progress -->\n- [x] tag\n",
        },
    ]
    layer = trust_stubs.FakeItems(
        {
            ("vergil-project/.github", 373): trust_stubs.item(
                body="<!-- vrg-release:progress -->\n- [x] tag\n"
            )
        },
        strict=True,
    )
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
        patch("vergil_tooling.lib.github.graphql", side_effect=layer),
        patch("vergil_tooling.lib.trust.load_trusted_authors", return_value=trust_stubs.AUTHORS),
    ):
        result = roadmap.gather("vergil-project")
    assert [e.number for e in result] == [40]
    assert layer.reads == [("vergil-project/.github", 373)]


def test_gather_returns_empty_on_non_list() -> None:
    with patch("vergil_tooling.lib.github.read_json", return_value={"unexpected": True}):
        assert roadmap.gather("vergil-project") == []


def _limited(rows: list[dict[str, object]]) -> Callable[..., list[dict[str, object]]]:
    """A ``gh issue list`` stand-in that returns at most ``--limit`` rows."""

    def fake(*args: str) -> list[dict[str, object]]:
        return rows[: int(args[args.index("--limit") + 1])]

    return fake


def _epics(count: int) -> list[dict[str, object]]:
    return [{"number": i, "title": f"Epic {i}", "labels": []} for i in range(1, count + 1)]


def test_open_epics_sees_an_epic_past_row_200() -> None:
    with patch("vergil_tooling.lib.github.read_json", side_effect=_limited(_epics(201))):
        assert len(roadmap._open_epics("org/.github")) == 201


def test_open_epics_refuses_a_list_that_may_be_truncated() -> None:
    with (
        patch(
            "vergil_tooling.lib.github.read_json",
            side_effect=_limited(_epics(epics.LISTING_LIMIT)),
        ),
        pytest.raises(epics.EpicListTruncatedError, match="open epics of org/.github"),
    ):
        roadmap._open_epics("org/.github")


def test_render_empty_says_none_in_each_section() -> None:
    out = roadmap.render([], [])
    assert out.index("## In flight") < out.index("## Planned") < out.index("## Completed")
    assert out.count("_None._") == 3


def test_render_splits_open_epics_into_in_flight_and_planned() -> None:
    summaries = [
        EpicSummary(
            40,
            "Convention | edge",
            "2026-06-28",
            None,
            ("vergil-project/.github", "vergil-project/vergil-tooling"),
            2,
            1,
            "u40",
        ),
        EpicSummary(7, "Orphan", "2026-02-02", None, (), 0, 0, "u7"),
    ]
    out = roadmap.render(summaries, [], "vergil-project")
    # Caption names the org the epics were read from.
    assert "the epics in vergil-project/.github" in out
    in_flight, rest = out.split("## Planned")
    planned, completed = rest.split("## Completed")
    # An epic with a closed task is in flight; one with none is planned.
    # Epic row: link + title (pipe escaped), Done as X/Y, repos stacked short
    # names via <br>, and the created date.
    assert (
        "| [#40](u40) Convention \\| edge | 1/2 | .github<br>vergil-tooling | 2026-06-28 |"
        in in_flight
    )
    # No repos renders an em dash in the cell.
    assert "| [#7](u7) Orphan | 0/0 | — | 2026-02-02 |" in planned
    # With no milestone anywhere, each section is one table and no subheading.
    assert "###" not in out
    assert out.count("| Epic | Done | Repos | Created |") == 2
    assert "_None._" in completed


def test_render_groups_a_section_by_milestone_when_any_has_one() -> None:
    summaries = [
        EpicSummary(40, "A", "2026-06-28", "v2.2", (), 2, 1, "u40"),
        EpicSummary(41, "B", "2026-06-29", None, (), 3, 1, "u41"),
        EpicSummary(7, "C", "2026-02-02", None, (), 1, 0, "u7"),
    ]
    out = roadmap.render(summaries, [])
    in_flight, planned = out.split("## Planned")
    assert in_flight.index("### v2.2") < in_flight.index("### No milestone")
    assert in_flight.count("| --- | --- | --- | --- |") == 2
    # The planned section has no milestone, so it stays ungrouped.
    assert "###" not in planned


def test_render_without_org_uses_generic_source() -> None:
    out = roadmap.render([EpicSummary(7, "Orphan", "2026-02-02", None, (), 0, 0, "u7")], [])
    assert "the epics in the org .github repo" in out


def _completed(number: int, closed: str, **kwargs: object) -> CompletedEpic:
    fields: dict[str, Any] = {
        "title": f"Epic {number}",
        "repos": (),
        "total": 0,
        "url": f"u{number}",
        "documents": (),
    }
    fields.update(kwargs)
    return CompletedEpic(number=number, closed=closed, **fields)


def test_render_lists_completed_epics_newest_first() -> None:
    completed = [
        _completed(10, "2026-07-01"),
        _completed(
            30,
            "2026-11-20",
            title="Shipped <b>",
            repos=("vergil-project/.github", "vergil-project/vergil-tooling"),
            total=12,
            documents=(("spec", "https://s"), ("retrospective", "https://r")),
        ),
        _completed(20, "2026-11-20"),
    ]
    out = roadmap.render([], completed)
    section = out.split("## Completed")[1]
    assert "| Epic | Closed | Tasks | Repos | Documents |" in section
    rows = [line for line in section.splitlines() if line.startswith("| [#")]
    # Most recently closed first; a same-day tie puts the higher number first.
    assert [row.split("]")[0] for row in rows] == ["| [#30", "| [#20", "| [#10"]
    assert rows[0] == (
        "| [#30](u30) Shipped &lt;b&gt; | 2026-11-20 | 12 | .github<br>vergil-tooling "
        "| [Spec](https://s) · [Retrospective](https://r) |"
    )
    # No documents and no repos render an em dash.
    assert rows[2] == "| [#10](u10) Epic 10 | 2026-07-01 | 0 | — | — |"


_DOCS_BODY = (
    "Intro.\n\n"
    "<!-- vrg-epic-docs -->\n"
    "**Documents**\n\n"
    "- Spec: [`epics/9-x/spec.md`](https://github.com/vergil-project/.github/blob/develop/epics/9-x/spec.md)\n"
    "- Plan: not written (predates the spec and plan convention)\n"
    "- Retrospective: [`epics/9-x/retrospective.md`](https://github.com/vergil-project/.github/blob/develop/epics/9-x/retrospective.md)\n"
    "<!-- /vrg-epic-docs -->\n"
)


def _hit(number: int, **kwargs: object) -> trust.TrustedHit:
    fields: dict[str, Any] = {
        "repo": "vergil-project/.github",
        "kind": "issue",
        "title": f"Epic {number}",
        "url": f"u{number}",
        "state": "CLOSED",
        "labels": ("epic",),
        "body": "",
        "body_role": trust.Role.AGENTS,
        "closed_at": "2026-11-20T10:00:00Z",
    }
    fields.update(kwargs)
    return trust.TrustedHit(number=number, **fields)


def test_gather_completed_reads_closed_epics_through_the_trust_layer() -> None:
    hits = [
        _hit(9, body=_DOCS_BODY),
        _hit(5, title="Epic (ad hoc): vergil-tooling", labels=("epic", "ad-hoc")),
        _hit(6, title="Epic (proxy): x", labels=("epic", "proxy")),
        _hit(8, title="[archived] old attempt"),
    ]
    skipped = [
        trust.Skipped(
            "vergil-project/.github", 3, "issue", "outsider", "2026-11-01", "body", trust.UNTRUSTED
        )
    ]
    children = [
        _child("vergil-project", "vergil-tooling", 1, "CLOSED"),
        _child("vergil-project", ".github", 2, "CLOSED"),
    ]
    with (
        patch("vergil_tooling.lib.trust.search_window", return_value=(hits, skipped)) as search,
        patch("vergil_tooling.lib.trust.report_skipped") as report,
        patch("vergil_tooling.lib.epics.child_states", return_value=children) as mock_children,
    ):
        result = roadmap.gather_completed("vergil-project")
    query = search.call_args.args[0]
    assert query == "repo:vergil-project/.github label:epic is:closed reason:completed"
    assert search.call_args.kwargs == {
        "kind": "issue",
        "field": "closed",
        "since": roadmap.COMPLETED_SINCE,
        "judge_close": True,
    }
    # The untrusted epics are reported, metadata only, never dropped silently.
    assert report.call_args.args[0] == skipped
    # Container, proxy and archived epics are left out.
    mock_children.assert_called_once_with(epics.IssueRef("vergil-project", ".github", 9))
    assert result == [
        CompletedEpic(
            number=9,
            title="Epic 9",
            closed="2026-11-20",
            repos=("vergil-project/.github", "vergil-project/vergil-tooling"),
            total=2,
            url="u9",
            documents=(
                (
                    "spec",
                    "https://github.com/vergil-project/.github/blob/develop/epics/9-x/spec.md",
                ),
                (
                    "retrospective",
                    "https://github.com/vergil-project/.github/blob/develop/epics/9-x/retrospective.md",
                ),
            ),
        )
    ]


def test_gather_completed_skips_release_tracking_issue() -> None:
    body = "<!-- vrg-release:progress -->\n- [x] tag\n"
    hits = [_hit(373, title="release: 2.1.4", body=body)]
    with (
        patch("vergil_tooling.lib.trust.search_window", return_value=(hits, [])),
        patch("vergil_tooling.lib.release.is_release_tracking_issue", return_value=True) as check,
        patch("vergil_tooling.lib.epics.child_states") as mock_children,
    ):
        assert roadmap.gather_completed("vergil-project") == []
    check.assert_called_once_with("vergil-project/.github", 373, title="release: 2.1.4", body=body)
    mock_children.assert_not_called()


def test_gather_completed_refuses_an_epic_with_no_close_time() -> None:
    with (
        patch(
            "vergil_tooling.lib.trust.search_window", return_value=([_hit(9, closed_at=None)], [])
        ),
        pytest.raises(trust.TrustReadError, match="u9 has no close time"),
    ):
        roadmap.gather_completed("vergil-project")


def test_gather_completed_fails_on_a_malformed_documents_section() -> None:
    body = "<!-- vrg-epic-docs -->\n**Documents**\n"
    with (
        patch("vergil_tooling.lib.trust.search_window", return_value=([_hit(9, body=body)], [])),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
        pytest.raises(epic_docs.EpicDocsFormatError, match="epic #9"),
    ):
        roadmap.gather_completed("vergil-project")


def test_gather_completed_epic_with_no_documents_section() -> None:
    with (
        patch("vergil_tooling.lib.trust.search_window", return_value=([_hit(9)], [])),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]),
    ):
        result = roadmap.gather_completed("vergil-project")
    assert result[0].documents == ()
    assert result[0].repos == ()


def test_gather_completed_defaults_org_to_current_repo_owner() -> None:
    with (
        patch("vergil_tooling.lib.github.current_org", return_value="logical-minds-foundry"),
        patch("vergil_tooling.lib.trust.search_window", return_value=([_hit(9)], [])) as search,
        patch("vergil_tooling.lib.epics.child_states", return_value=[]) as mock_children,
    ):
        roadmap.gather_completed()
    assert search.call_args.args[0].startswith("repo:logical-minds-foundry/.github ")
    assert mock_children.call_args.args[0].owner == "logical-minds-foundry"


def test_gather_defaults_org_to_current_repo_owner() -> None:
    epic_list = [
        {
            "number": 9,
            "title": "Y",
            "createdAt": "2026-03-03T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u9",
        }
    ]
    with (
        patch("vergil_tooling.lib.github.current_org", return_value="logical-minds-foundry"),
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list) as mock_read,
        patch("vergil_tooling.lib.epics.child_states", return_value=[]) as mock_children,
    ):
        result = roadmap.gather()
    assert result[0].number == 9
    # The derived org drives both the epic query and the child rollup.
    assert "logical-minds-foundry/.github" in mock_read.call_args.args
    assert mock_children.call_args.args[0].owner == "logical-minds-foundry"


# Hostile titles: tag-like, script-like, link-like and a pipe.
_HOSTILE_TITLES = (
    ("<b>bold</b>", "&lt;b&gt;bold&lt;/b&gt;"),
    ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
    ("[x](javascript:alert(1))", "\\[x\\](javascript:alert(1))"),
    ("a|b", "a\\|b"),
)


def test_roadmap_row_escapes_title() -> None:
    for title, escaped in _HOSTILE_TITLES:
        row = roadmap._row(EpicSummary(9, title, "2026-06-28", None, (), 0, 0, "u9"))
        prefix = "| [#9](u9) "
        assert row == f"{prefix}{escaped} | 0/0 | — | 2026-06-28 |"
        assert "<" not in row
        # The pipe is escaped exactly once.
        assert "\\\\|" not in row


def test_render_escapes_milestone_title() -> None:
    summary = EpicSummary(9, "t", "2026-06-28", "<i>v3</i> | *x*", (), 0, 0, "u9")
    out = roadmap.render([summary], [], "vergil-project")
    assert "### &lt;i&gt;v3&lt;/i&gt; \\| \\*x\\*" in out
    assert "<i>" not in out


def test_gather_excludes_proxy() -> None:
    # A proxy is not a development epic of its own (#1417): the roadmap lists
    # its primary, never the proxy.
    epic_list = [
        {
            "number": 40,
            "title": "Convention",
            "createdAt": "2026-06-28T10:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}],
            "url": "u40",
        },
        {
            "number": 9,
            "title": "Epic (proxy): keep ad-hoc queues standalone",
            "createdAt": "2026-01-01T00:00:00Z",
            "milestone": None,
            "labels": [{"name": "epic"}, {"name": "proxy"}],
            "url": "u9",
        },
    ]
    with (
        patch("vergil_tooling.lib.github.read_json", return_value=epic_list),
        patch("vergil_tooling.lib.epics.child_states", return_value=[]) as mock_children,
    ):
        result = roadmap.gather("vergil-project")
    assert [s.number for s in result] == [40]
    mock_children.assert_called_once_with(epics.IssueRef("vergil-project", ".github", 40))


# -- chained epics (epic vergil-project/.github#1609) --------------------------------

_HEAD = epics.IssueRef("vergil-project", ".github", 40)
_C2 = epics.IssueRef("vergil-project", ".github", 41)


def _chained_graph(monkeypatch: pytest.MonkeyPatch, *, state: str) -> FakeSubIssueGraph:
    """``_HEAD`` holding 99 items and ``_C2`` in its last slot with 2 more, all in *state*."""
    graph = FakeSubIssueGraph().install(monkeypatch)
    graph.add_issue(_HEAD, title="Convention", labels=["epic", "epic-development"], state=state)
    for number in range(1000, 1000 + epic_chain.ITEM_CAP):
        item = graph.add_issue(epics.IssueRef("vergil-project", "vergil-tooling", number), title="t")
        graph.link(_HEAD, item)
    graph.add_continuation(_C2, _HEAD, _HEAD)
    for number in (1, 2):
        item = graph.add_issue(epics.IssueRef("vergil-project", ".github", 900 + number), title="t")
        graph.link(_C2, item)
    if state == "CLOSED":
        for ref in graph.issues:
            graph.close(ref)
    graph.clear_log()
    monkeypatch.setattr(epics, "resolve_epic_home", lambda org, _repo: f"{org}/.github")
    monkeypatch.setattr("vergil_tooling.lib.release.is_release_tracking_issue", lambda *a, **k: False)
    return graph


def _row(ref: epics.IssueRef, graph: FakeSubIssueGraph) -> dict[str, Any]:
    issue = graph.issues[ref]
    return {
        "number": ref.number,
        "title": issue.title,
        "createdAt": "2026-06-28T10:00:00Z",
        "milestone": None,
        "labels": [{"name": name} for name in issue.labels],
        "url": f"u{ref.number}",
        "body": issue.body,
    }


def test_roadmap_skips_continuations(monkeypatch: pytest.MonkeyPatch) -> None:
    # The head summarizes the whole chain; its continuation is never an epic of its own.
    graph = _chained_graph(monkeypatch, state="OPEN")
    rows = [_row(_HEAD, graph), _row(_C2, graph)]
    monkeypatch.setattr(roadmap, "_open_epics", lambda _home: rows)
    (summary,) = roadmap.gather("vergil-project")
    assert summary.number == _HEAD.number
    assert summary.total == epic_chain.ITEM_CAP + 2
    assert summary.repos == ("vergil-project/.github", "vergil-project/vergil-tooling")
    assert graph.events == []


def test_roadmap_completed_skips_continuations(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = _chained_graph(monkeypatch, state="CLOSED")
    hits = [
        _hit(_HEAD.number, title="Convention", labels=("epic", "epic-development")),
        _hit(_C2.number, title="Convention (continuation 2)", labels=("epic", "continuation")),
    ]
    with patch("vergil_tooling.lib.trust.search_window", return_value=(hits, [])):
        (completed,) = roadmap.gather_completed("vergil-project")
    assert completed.number == _HEAD.number
    assert completed.total == epic_chain.ITEM_CAP + 2
    assert graph.events == []
