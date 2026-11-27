"""Tests for the release-group record: its grammar, the latest-revision rule and the trust rules.

Epic vergil-project/.github#1672, phase 1 Task 2 (spec §5.1, §5.9). The epic's comment stream
is stubbed at ``trust.read_comment_stream``; the epic's kind, its proxy links and the primary
epic's kind are stubbed at their readers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib import epic_audit, epics, proxies, trust
from vergil_tooling.lib.epics import IssueRef, TaskEpic
from vergil_tooling.lib.release_groups import record
from vergil_tooling.lib.release_groups.record import (
    GroupRecordError,
    ParsedRecord,
    groups_of,
    parse_record,
    read_log,
    render_record,
)
from vergil_tooling.lib.trust import CommentStream, Role, Skipped, StreamComment

if TYPE_CHECKING:
    from collections.abc import Sequence

EPIC = IssueRef("org", "repo", 40)
CONTINUATION = IssueRef("org", "repo", 41)
PRIMARY = IssueRef("org", "repo", 50)
CREATED = "2026-11-01T09:30:00Z"


def ref(number: int) -> IssueRef:
    """A task of the test epic's repo."""
    return IssueRef("org", "repo", number)


def _record(group: str, rev: int, *members: int) -> str:
    return f"release-group {group} rev {rev}: " + " ".join(f"org/repo#{n}" for n in members)


def _dissolved(group: str, rev: int) -> str:
    return f"release-group {group} rev {rev}: dissolved"


def _comment(cid: int, body: str, *, edited: bool = False) -> StreamComment:
    return StreamComment(
        id=str(cid), login="human", body=body, role=Role.HUMAN, at=CREATED, edited=edited
    )


def _outsider(cid: int, *, edit: bool = False) -> Skipped:
    """Metadata of an untrusted comment (or of an outsider's edit of a trusted one)."""
    return Skipped(
        repo="org/repo",
        number=40,
        kind="edit" if edit else "comment",
        login="drive-by",
        at=CREATED,
        field=f"comment {cid}",
        cause=trust.UNTRUSTED,
    )


def _assert_epic_refused(log: record.GroupLog, comment: str) -> None:
    """The log refuses the whole epic, and the refusal names *comment*."""
    assert log.epic_error is not None
    assert comment in str(log.epic_error)
    assert (log.groups, log.broken) == ({}, {})


@dataclass
class FakeEpic:
    """The epic under test: its comments, its kind and its proxy link, all stubbed."""

    comments: list[StreamComment] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    kind: str = epics.DEVELOPMENT_KIND
    head: IssueRef = EPIC
    unparented: frozenset[int] = frozenset()
    proxy_problems: list[str] = field(default_factory=list)
    primary_kind: str = epics.DEVELOPMENT_KIND
    primary_error: Exception | None = None
    stream_reads: list[tuple[str, int]] = field(default_factory=list)

    def stream(self, repo: str, number: int, **_: object) -> CommentStream:
        self.stream_reads.append((repo, number))
        ids = {c.id for c in self.comments}
        ids |= {s.field.removeprefix("comment ") for s in self.skipped}
        return CommentStream(
            comments=tuple(self.comments), ids=frozenset(ids), skipped=tuple(self.skipped)
        )

    def epic_kind(self, epic: IssueRef) -> tuple[IssueRef, str]:
        return (epic, self.primary_kind if epic == PRIMARY else self.kind)

    def task_epic(self, task: IssueRef) -> TaskEpic:
        if task.number in self.unparented:
            return TaskEpic(task, None, epics.NO_PARENT_KIND, None)
        return TaskEpic(task, self.head, self.kind, EPIC)

    def primary_of(self, proxy: IssueRef) -> IssueRef:
        if self.primary_error is not None:
            raise self.primary_error
        return PRIMARY


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEpic:
    epic = FakeEpic()
    monkeypatch.setattr(trust, "read_comment_stream", epic.stream)
    monkeypatch.setattr(epics, "epic_kind", epic.epic_kind)
    monkeypatch.setattr(epics, "task_epic", epic.task_epic)
    monkeypatch.setattr(epic_audit, "proxy_link_problems", lambda _e: list(epic.proxy_problems))
    monkeypatch.setattr(proxies, "primary_of", epic.primary_of)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    return epic


# --- the grammar --------------------------------------------------------------


def test_parse_record_members_and_dissolved() -> None:
    assert parse_record(_record("G1", 3, 1, 2)) == ParsedRecord(
        group="G1", rev=3, members=(ref(1), ref(2)), dissolved=False, reason=None
    )
    assert parse_record(_dissolved("G2", 2) + "\nReason: split 13") == ParsedRecord(
        group="G2", rev=2, members=(), dissolved=True, reason="split 13"
    )


def test_parse_record_not_a_record_is_none() -> None:
    assert parse_record("just a comment\nrelease-group G1 rev 1: org/repo#1") is None
    assert parse_record("") is None


@pytest.mark.parametrize(
    "body",
    [
        "release-group G01 rev 1: org/repo#1 org/repo#2",
        "release-group G1 rev 0: org/repo#1 org/repo#2",
        "release-group G1 rev 01: org/repo#1 org/repo#2",
        "release-group G1 rev 1: #5 org/repo#2",
        "release-group G1 rev 1: org/repo#1 org/repo#1",
        "release-group G1 rev 1: org/repo#1 org/repo#2 trailing",
        "release-group G1 rev 1: org/repo#1\nReason: one\nReason: two",
        "release-group G1 rev x: org/repo#1 org/repo#2",
    ],
)
def test_parse_record_malformed_raises(body: str) -> None:
    with pytest.raises(GroupRecordError):
        parse_record(body)


def test_render_record_round_trips() -> None:
    members = (ref(1), ref(2))
    text = render_record("G1", 3, members, reason="split 13")
    assert text == "release-group G1 rev 3: org/repo#1 org/repo#2\nReason: split 13"
    assert parse_record(text) == ParsedRecord("G1", 3, members, False, "split 13")
    dissolved = render_record("G2", 2, (), dissolved=True)
    assert dissolved == "release-group G2 rev 2: dissolved"
    assert parse_record(dissolved) == ParsedRecord("G2", 2, (), True, None)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"group": "G0", "rev": 1, "members": (ref(1),)}, "not a group ID"),
        ({"group": "G1", "rev": 0, "members": (ref(1),)}, "positive"),
        ({"group": "G1", "rev": 1, "members": ()}, "names no members"),
        ({"group": "G1", "rev": 1, "members": (ref(1), ref(1))}, "more than once"),
        ({"group": "G1", "rev": 1, "members": (ref(1),), "dissolved": True}, "dissolved"),
        ({"group": "G1", "rev": 1, "members": (ref(1),), "reason": "a\nb"}, "one non-empty"),
        ({"group": "G1", "rev": 1, "members": (ref(1),), "reason": "  "}, "one non-empty"),
    ],
)
def test_render_record_rejects_bad_input(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        render_record(**kwargs)  # type: ignore[arg-type]


# --- the log: revisions, conflicts, edits -------------------------------------


def test_latest_revision_wins(fake: FakeEpic) -> None:
    fake.comments = [
        _comment(1, _record("G1", 1, 1, 2)),
        _comment(2, _record("G1", 2, 1, 3)),
        _comment(3, _record("G1", 3, 4, 5)),
    ]
    group = read_log(EPIC).groups["G1"]
    assert (group.rev, group.members, group.comment_id) == (3, (ref(4), ref(5)), "3")


def test_conflict_at_top_revision_refuses(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, _record("G1", 4, 1, 2)), _comment(2, _record("G1", 4, 1, 3))]
    with pytest.raises(GroupRecordError, match="comment 1 and comment 2"):
        groups_of(ref(1))
    fake.comments = [_comment(2, _record("G1", 4, 1, 3))]
    assert [g.rev for g in groups_of(ref(1))] == [4]


def test_out_of_order_revision_refuses(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, _record("G1", 4, 1, 2)), _comment(2, _record("G1", 3, 1, 5))]
    with pytest.raises(GroupRecordError, match="comment 2 sets revision 3 after comment 1"):
        groups_of(ref(1))


def test_edited_record_refuses(fake: FakeEpic) -> None:
    fake.comments = [
        _comment(1, _record("G1", 3, 1, 2)),
        _comment(2, _record("G1", 4, 1, 3), edited=True),
    ]
    with pytest.raises(GroupRecordError, match="comment 2 was edited"):
        groups_of(ref(1))
    fake.comments = [_comment(1, _record("G1", 3, 1, 2))]
    assert [g.rev for g in groups_of(ref(1))] == [3]


@pytest.mark.parametrize("body", [_record("G1", 1, 1, 2), "a plain comment"])
def test_untrusted_edit_of_trusted_comment_refuses_epic(fake: FakeEpic, body: str) -> None:
    fake.comments = [_comment(1, body)]
    fake.skipped = [_outsider(1, edit=True)]
    with pytest.raises(GroupRecordError, match="comment 1"):
        groups_of(ref(1))
    with pytest.raises(GroupRecordError, match="comment 1"):
        groups_of(ref(9))  # a standalone task of the epic is refused too
    _assert_epic_refused(read_log(EPIC), "comment 1")


def test_revision_gap_reads(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, _record("G1", 1, 1, 2)), _comment(3, _record("G1", 3, 3, 4))]
    assert [g.rev for g in groups_of(ref(3))] == [3]


def test_broken_group_may_hold_conflict_side(fake: FakeEpic) -> None:
    fake.comments = [
        _comment(1, _record("G1", 3, 1, 2)),
        _comment(2, _record("G1", 4, 1, 2, 7)),
        _comment(3, _record("G1", 4, 1, 2, 3)),
    ]
    with pytest.raises(GroupRecordError, match="G1"):
        groups_of(ref(7))
    assert groups_of(ref(9)) == []


def test_broken_group_may_hold_edited_text(fake: FakeEpic) -> None:
    fake.comments = [
        _comment(1, _record("G1", 3, 1, 7)),
        _comment(2, _record("G1", 4, 1, 2), edited=True),
    ]
    with pytest.raises(GroupRecordError, match="comment 1"):
        groups_of(ref(7))


def test_group_with_no_readable_record_refuses_every_task(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, "release-group G1 rev x: org/repo#1")]
    with pytest.raises(GroupRecordError, match="comment 1"):
        groups_of(ref(9))


def test_multiple_reason_lines_refuse_the_group(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, _record("G1", 1, 1, 2) + "\nReason: a\nReason: b")]
    with pytest.raises(GroupRecordError, match="comment 1"):
        groups_of(ref(1))


# --- trust and scope ----------------------------------------------------------


def test_untrusted_record_ignored_and_reported(
    fake: FakeEpic, capsys: pytest.CaptureFixture[str]
) -> None:
    fake.comments = [_comment(1, _record("G1", 1, 1, 2))]
    fake.skipped = [_outsider(2)]
    log = read_log(EPIC)
    assert log.groups["G1"].rev == 1
    assert log.groups["G1"].members == (ref(1), ref(2))
    assert "comment 2" in capsys.readouterr().err


def test_conflict_in_other_group_does_not_block(fake: FakeEpic) -> None:
    fake.comments = [
        _comment(1, _record("G1", 1, 1, 2)),
        _comment(2, _record("G2", 1, 3, 4)),
        _comment(3, _record("G2", 1, 3, 5)),
    ]
    assert [g.group for g in groups_of(ref(1))] == ["G1"]


def test_standalone_task_unaffected_by_broken_group(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, _record("G1", 4, 1, 2)), _comment(2, _record("G1", 4, 1, 3))]
    assert groups_of(ref(9)) == []


def test_unscoped_failure_refuses_every_read(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, "release-group G01 rev 1: org/repo#1")]
    with pytest.raises(GroupRecordError, match="comment 1"):
        groups_of(ref(9))
    _assert_epic_refused(read_log(EPIC), "comment 1")


def test_member_in_two_live_groups_refuses(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, _record("G1", 1, 1, 2)), _comment(2, _record("G2", 1, 1, 3))]
    with pytest.raises(GroupRecordError, match="more than one live group"):
        groups_of(ref(1))
    assert [g.group for g in groups_of(ref(2))] == ["G1"]


def test_dissolved_id_stays_used(fake: FakeEpic) -> None:
    fake.comments = [_comment(1, _record("G1", 1, 1, 2)), _comment(2, _dissolved("G1", 2))]
    log = read_log(EPIC)
    assert "G1" in log.ids_used
    assert log.groups["G1"].dissolved
    assert groups_of(ref(1)) == []


def test_groups_of_resolves_continuation_to_head(fake: FakeEpic) -> None:
    fake.head = EPIC
    fake.comments = [_comment(1, _record("G1", 1, 1, 2))]
    assert [g.group for g in groups_of(ref(1))] == ["G1"]
    assert fake.stream_reads == [("org/repo", 40)]


def test_groups_of_task_without_epic_is_empty(fake: FakeEpic) -> None:
    fake.unparented = frozenset({9})
    assert groups_of(ref(9)) == []
    assert fake.stream_reads == []


def test_no_records_reads_as_empty_log_on_any_kind(fake: FakeEpic) -> None:
    fake.kind = "ad-hoc"
    fake.comments = [_comment(1, "an ordinary comment")]
    log = read_log(EPIC)
    assert (log.groups, log.broken, log.epic_error, log.ids_used) == ({}, {}, None, frozenset())
    assert groups_of(ref(1)) == []


# --- the epic must be a group's epic ------------------------------------------


def test_record_on_ad_hoc_epic_refuses(fake: FakeEpic) -> None:
    fake.kind = "ad-hoc"
    fake.comments = [_comment(1, _record("G1", 1, 1, 2))]
    with pytest.raises(GroupRecordError, match=r"org/repo#40 cannot be read: it holds"):
        groups_of(ref(1))


def test_record_on_management_epic_refuses(fake: FakeEpic) -> None:
    fake.kind = "operations"
    fake.comments = [_comment(1, _record("G1", 1, 1, 2))]
    with pytest.raises(GroupRecordError, match="operations"):
        groups_of(ref(1))


def test_record_on_queue_epic_refuses(fake: FakeEpic) -> None:
    fake.kind = "planning"
    fake.comments = [_comment(1, _record("G1", 1, 1, 2))]
    with pytest.raises(GroupRecordError, match="planning"):
        groups_of(ref(1))


def test_group_on_proxy_epic_reads(fake: FakeEpic) -> None:
    fake.kind = "proxy"
    fake.comments = [_comment(1, _record("G1", 2, 1, 2))]
    groups = groups_of(ref(1))
    assert [(g.group, g.epic) for g in groups] == [("G1", EPIC)]


@pytest.mark.parametrize(
    ("problems", "primary_kind", "primary_error"),
    [
        (["the Primary line is missing"], epics.DEVELOPMENT_KIND, None),
        ([], "ad-hoc", None),
        ([], epics.DEVELOPMENT_KIND, proxies.ProxyLinkError("no Primary line")),
    ],
)
def test_record_on_proxy_with_broken_link_refuses(
    fake: FakeEpic,
    problems: list[str],
    primary_kind: str,
    primary_error: Exception | None,
) -> None:
    fake.kind = "proxy"
    fake.proxy_problems = problems
    fake.primary_kind = primary_kind
    fake.primary_error = primary_error
    fake.comments = [_comment(1, _record("G1", 1, 1, 2))]
    with pytest.raises(GroupRecordError, match="org/repo#40"):
        groups_of(ref(1))


def test_proxy_without_records_skips_the_link_check(fake: FakeEpic) -> None:
    fake.kind = "proxy"
    fake.proxy_problems = ["would refuse if checked"]
    assert groups_of(ref(1)) == []
