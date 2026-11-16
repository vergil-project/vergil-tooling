"""Tests for the epic-size scale, sizing comment and size record (lib/epic_size.py)."""

from __future__ import annotations

from datetime import date
from typing import cast

import pytest

from vergil_tooling.lib import epic_size
from vergil_tooling.lib.epic_size import (
    LABEL_PREFIX,
    SIZES,
    Confidence,
    SizeRecord,
    Sizing,
    has_size,
    label_for,
    render_cleared_marker,
    render_sizing_comment,
    size_for_tasks,
    size_record,
    trusted_logins,
)
from vergil_tooling.lib.trusted_authors import TrustedAuthors, TrustedAuthorsNotConfiguredError

DAY = date(2026, 11, 16)
TRUSTED = frozenset({"vergil-user", "human"})

M14 = (
    "Sized 2026-11-16: epic-size M, about 14 tasks.\n"
    "Epic-size: M\n"
    "Estimated-tasks: 14\n"
    "Confidence: med\n"
    "Reason: One sentence."
)
S5 = (
    "Sized 2026-11-17: epic-size S, about 5 tasks.\n"
    "Epic-size: S\n"
    "Estimated-tasks: 5\n"
    "Confidence: high\n"
    "Reason: Small."
)
XL50 = (
    "Sized 2026-11-17: epic-size XL, about 50 tasks.\n"
    "Epic-size: XL\n"
    "Estimated-tasks: 50\n"
    "Confidence: low\n"
    "Reason: Forged."
)
CLEARED = "Size cleared 2026-11-17: moved out of planning as a one-off."


def _assert_error(record: SizeRecord) -> None:
    assert record.size is None
    assert record.tasks is None
    assert record.confidence is None
    assert record.error
    assert record.display == "!"


def test_bands() -> None:
    assert size_for_tasks(1) == "S"
    assert size_for_tasks(9) == "S"
    assert size_for_tasks(10) == "M"
    assert size_for_tasks(19) == "M"
    assert size_for_tasks(20) == "L"
    assert size_for_tasks(39) == "L"
    assert size_for_tasks(40) == "XL"
    assert size_for_tasks(400) == "XL"
    for bad in (0, -1):
        with pytest.raises(ValueError, match="positive"):
            size_for_tasks(bad)


def test_sizes_and_labels() -> None:
    assert SIZES == ("S", "M", "L", "XL")
    assert LABEL_PREFIX == "epic-size:"
    assert [label_for(s) for s in SIZES] == [
        "epic-size:S",
        "epic-size:M",
        "epic-size:L",
        "epic-size:XL",
    ]


def test_render_sizing_comment() -> None:
    sizing = Sizing(14, "med", "One sentence.")
    assert sizing.size == "M"
    assert render_sizing_comment(sizing, DAY) == M14


def test_render_cleared_marker() -> None:
    assert render_cleared_marker(date(2026, 11, 17)) == CLEARED


def test_sizing_rejects() -> None:
    with pytest.raises(ValueError, match="reason"):
        Sizing(14, "med", "  ")
    with pytest.raises(ValueError, match="reason"):
        Sizing(14, "med", "Two\nlines.")
    with pytest.raises(ValueError, match="confidence"):
        Sizing(14, cast("Confidence", "medium"), "One sentence.")
    with pytest.raises(ValueError, match="positive"):
        Sizing(0, "med", "One sentence.")


def test_record_unsized() -> None:
    record = size_record([], [], TRUSTED)
    assert record == SizeRecord(None, None, None, None)
    assert record.display == "?"
    # Other labels and other comments leave it unsized.
    assert size_record(["feature"], [("vergil-user", "hello")], TRUSTED).display == "?"


def test_record_sized() -> None:
    record = size_record(["planning", "epic-size:M"], [("app/vergil-user", M14)], TRUSTED)
    assert record == SizeRecord("M", 14, "med", None)
    assert record.display == "M/14"


def test_record_latest_trusted_wins() -> None:
    comments = [("vergil-user", S5), ("human", M14)]
    assert size_record(["epic-size:M"], comments, TRUSTED) == SizeRecord("M", 14, "med", None)


def test_record_comment_without_label() -> None:
    record = size_record([], [("vergil-user", M14)], TRUSTED)
    _assert_error(record)
    assert "without label" in (record.error or "")


def test_record_label_without_comment() -> None:
    record = size_record(["epic-size:M"], [], TRUSTED)
    _assert_error(record)
    assert "without" in (record.error or "")
    assert "comment" in (record.error or "")


def test_record_disagreement() -> None:
    record = size_record(["epic-size:L"], [("vergil-user", M14)], TRUSTED)
    _assert_error(record)
    assert "epic-size:L" in (record.error or "")
    assert "M" in (record.error or "")


def test_record_several_labels() -> None:
    record = size_record(["epic-size:M", "epic-size:S"], [("vergil-user", M14)], TRUSTED)
    _assert_error(record)
    assert "several" in (record.error or "")


def test_record_unknown_size_label() -> None:
    record = size_record(["epic-size:XXL"], [("vergil-user", M14)], TRUSTED)
    _assert_error(record)
    assert "epic-size:XXL" in (record.error or "")


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (M14.replace("\nReason: One sentence.", ""), "Reason"),
        (M14.replace("Estimated-tasks: 14", "Estimated-tasks: fourteen"), "Estimated-tasks"),
        (M14.replace("Confidence: med", "Confidence: medium"), "Confidence"),
        (M14.replace("Epic-size: M", "Epic-size: S"), "Epic-size"),
        (M14.replace("Epic-size: M", "Epic-size: XXL"), "Epic-size"),
        (M14.replace("Estimated-tasks: 14", "Estimated-tasks: 0"), "Estimated-tasks"),
        (M14.replace("Reason: One sentence.", "Reason:   "), "Reason"),
        (M14 + "\nConfidence: high", "Confidence"),
    ],
)
def test_record_malformed(body: str, fragment: str) -> None:
    record = size_record(["epic-size:M"], [("vergil-user", body)], TRUSTED)
    _assert_error(record)
    assert "malformed" in (record.error or "")
    assert fragment in (record.error or "")


def test_record_malformed_key_must_start_the_line() -> None:
    body = M14.replace("Reason: One sentence.", "  Reason: One sentence.")
    _assert_error(size_record(["epic-size:M"], [("vergil-user", body)], TRUSTED))


def test_latest_malformed_is_not_skipped() -> None:
    malformed = M14.replace("Confidence: med", "Confidence: medium")
    comments = [("vergil-user", M14), ("vergil-user", malformed)]
    record = size_record(["epic-size:M"], comments, TRUSTED)
    _assert_error(record)
    assert "malformed" in (record.error or "")


def test_untrusted_sizing_ignored() -> None:
    comments = [("vergil-user", M14), ("mallory", XL50), ("app/mallory", XL50)]
    assert size_record(["epic-size:M"], comments, TRUSTED) == SizeRecord("M", 14, "med", None)
    # An untrusted malformed comment is ignored too, and alone it sizes nothing.
    malformed = M14.replace("Confidence: med", "Confidence: medium")
    assert size_record([], [("mallory", malformed)], TRUSTED).display == "?"


def test_cleared_marker_resets() -> None:
    cleared = [("vergil-user", M14), ("human", CLEARED)]
    assert size_record([], cleared, TRUSTED) == SizeRecord(None, None, None, None)
    resized = [*cleared, ("vergil-user", S5)]
    assert size_record(["epic-size:S"], resized, TRUSTED) == SizeRecord("S", 5, "high", None)
    # A label left behind after a clear is a label without a comment.
    _assert_error(size_record(["epic-size:M"], cleared, TRUSTED))
    # An untrusted cleared marker is ignored.
    forged = [("vergil-user", M14), ("mallory", CLEARED)]
    assert size_record(["epic-size:M"], forged, TRUSTED) == SizeRecord("M", 14, "med", None)


def test_has_size() -> None:
    assert has_size(["epic-size:M"], [])
    assert has_size([], ["note", M14])
    assert has_size([], [XL50])  # any author: the bodies carry none
    assert not has_size(["feature"], ["note", CLEARED, "  "])
    assert not has_size([], [])


def test_trusted_logins(monkeypatch: pytest.MonkeyPatch) -> None:
    authors = TrustedAuthors(human=("human",), agents=("vergil-user", "vergil-audit"))
    monkeypatch.setattr(epic_size, "load_trusted_authors", lambda: authors)
    assert trusted_logins() == frozenset({"human", "vergil-user", "vergil-audit"})


def test_trusted_logins_unconfigured_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def unconfigured() -> TrustedAuthors:
        msg = "not configured"
        raise TrustedAuthorsNotConfiguredError(msg)

    monkeypatch.setattr(epic_size, "load_trusted_authors", unconfigured)
    with pytest.raises(TrustedAuthorsNotConfiguredError):
        trusted_logins()
