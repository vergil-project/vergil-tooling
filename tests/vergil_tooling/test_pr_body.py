"""Tests for vergil_tooling.lib.pr_body."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import pr_body


def test_build_pr_body_full() -> None:
    body = pr_body.build_pr_body(
        summary="Fix the thing",
        linkage="Ref",
        issue_ref="#42",
        notes="Tested locally",
    )
    assert "# Pull Request" in body
    assert "## Summary\n\n- Fix the thing" in body
    assert "## Issue Linkage\n\n- Ref #42" in body
    assert "## Notes\n\n- Tested locally" in body


def test_build_pr_body_empty_notes_renders_dash() -> None:
    body = pr_body.build_pr_body(
        summary="Fix the thing",
        linkage="Ref",
        issue_ref="#42",
        notes="",
    )
    assert body.endswith("## Notes\n\n- -")


def test_resolve_issue_ref_plain_number() -> None:
    assert pr_body.resolve_issue_ref("42") == "#42"


def test_resolve_issue_ref_cross_repo() -> None:
    assert pr_body.resolve_issue_ref("owner/repo#42") == "owner/repo#42"


def test_resolve_issue_ref_invalid() -> None:
    with pytest.raises(SystemExit, match="must be a number"):
        pr_body.resolve_issue_ref("bad-ref")


def test_resolve_issue_ref_zero() -> None:
    with pytest.raises(SystemExit, match="must be a number"):
        pr_body.resolve_issue_ref("0")


def test_build_pr_body_rejects_linkage_keyword_in_notes() -> None:
    with pytest.raises(SystemExit) as exc:
        pr_body.build_pr_body(summary="s", linkage="Closes", issue_ref="#42", notes="Ref #99")
    assert "Ref #99" in str(exc.value)


def test_build_pr_body_rejects_linkage_keyword_in_summary() -> None:
    with pytest.raises(SystemExit) as exc:
        pr_body.build_pr_body(summary="Closes #7", linkage="Closes", issue_ref="#42", notes="")
    assert "Closes #7" in str(exc.value)


def test_build_pr_body_allows_bare_reference_in_notes() -> None:
    body = pr_body.build_pr_body(
        summary="s", linkage="Closes", issue_ref="#42", notes="See #99 for background"
    )
    assert "See #99 for background" in body
