"""Tests for vergil_tooling.lib.trusted_comments (trusted comment bodies)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import vergil_tooling.lib.github
from tests.vergil_tooling import trust_stubs
from vergil_tooling.lib import trust, trusted_comments
from vergil_tooling.lib.epics import IssueRef

if TYPE_CHECKING:
    from tests.vergil_tooling.trust_stubs import FakeItems

REF = IssueRef("org", "tool", 3)


@pytest.fixture
def items(monkeypatch: pytest.MonkeyPatch) -> FakeItems:
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    fake = trust_stubs.FakeItems(strict=True)
    monkeypatch.setattr(vergil_tooling.lib.github, "graphql", fake)
    return fake


def test_bodies_keep_trusted_comments_in_order_and_report_the_rest(
    items: FakeItems, capsys: pytest.CaptureFixture[str]
) -> None:
    items.items[("org/tool", 3)] = trust_stubs.item(
        nodes=[
            trust_stubs.comment(1, "first"),
            trust_stubs.comment(2, trust_stubs.SPAM_TEXT, trust_stubs.OUTSIDER),
            trust_stubs.comment(3, "second", trust_stubs.AGENT),
        ]
    )
    assert trusted_comments.bodies(REF, trust_stubs.AUTHORS) == ["first", "second"]
    err = capsys.readouterr().err
    assert err == (
        f"skipped untrusted content: org/tool#3 comment drive-by at {trust_stubs.CREATED}"
        " on comment 2 (untrusted)\n"
    )
    assert "SPAM-MARKER-TEXT" not in err


def test_untrusted_body_is_not_reported(
    items: FakeItems, capsys: pytest.CaptureFixture[str]
) -> None:
    # The callers take nothing from the body through this read.
    items.items[("org/tool", 3)] = trust_stubs.item(
        author=trust_stubs.OUTSIDER, nodes=trust_stubs.comments("a note")
    )
    assert trusted_comments.bodies(REF, trust_stubs.AUTHORS) == ["a note"]
    assert capsys.readouterr().err == ""


def test_bodies_load_the_configured_authors(
    items: FakeItems, monkeypatch: pytest.MonkeyPatch
) -> None:
    items.items[("org/tool", 3)] = trust_stubs.item(nodes=trust_stubs.comments("a note"))
    monkeypatch.setattr(trust, "load_trusted_authors", lambda: trust_stubs.AUTHORS)
    assert trusted_comments.bodies(REF) == ["a note"]


def test_read_failure_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vergil_tooling.lib.github, "graphql", lambda query, **v: {})
    with pytest.raises(trust.TrustReadError, match="read_issue org/tool#3"):
        trusted_comments.bodies(REF, trust_stubs.AUTHORS)
