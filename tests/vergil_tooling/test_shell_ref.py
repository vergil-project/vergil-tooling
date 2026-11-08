"""Tests for the paste-ready issue-ref quoting in ``lib.shell_ref`` (#3966)."""

from __future__ import annotations

from vergil_tooling.lib.epics import IssueRef
from vergil_tooling.lib.shell_ref import quoted_issue_ref, quoted_slug


def test_quoted_issue_ref_single_quotes_the_ref() -> None:
    assert quoted_issue_ref("org/.github", 42) == "'org/.github#42'"


def test_quoted_slug_quotes_an_issue_ref() -> None:
    assert quoted_slug(IssueRef(owner="org", repo="tool", number=7)) == "'org/tool#7'"
