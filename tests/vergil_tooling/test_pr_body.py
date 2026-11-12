"""Tests for vergil_tooling.lib.pr_body."""

from __future__ import annotations

import pytest

from vergil_tooling.lib import pr_body
from vergil_tooling.lib.linkage import extract_tracking_refs


def test_build_pr_body_full() -> None:
    body = pr_body.build_pr_body(
        summary="Fix the thing",
        links=[("Ref", "#42")],
        notes="Tested locally",
    )
    assert "# Pull Request" in body
    assert "## Summary\n\n- Fix the thing" in body
    assert "## Issue Linkage\n\n- Ref #42" in body
    assert "## Notes\n\n- Tested locally" in body


def test_build_pr_body_empty_notes_renders_dash() -> None:
    body = pr_body.build_pr_body(
        summary="Fix the thing",
        links=[("Ref", "#42")],
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


def test_normalize_issue_ref_plain_number() -> None:
    assert pr_body.normalize_issue_ref("42") == "#42"


def test_normalize_issue_ref_cross_repo() -> None:
    assert pr_body.normalize_issue_ref("owner/repo#42") == "owner/repo#42"


def test_normalize_issue_ref_invalid_raises_value_error() -> None:
    # Unlike resolve_issue_ref (SystemExit), the shared normalizer raises
    # ValueError so each caller surfaces it in its own error convention (#2213).
    with pytest.raises(ValueError, match="must be a number"):
        pr_body.normalize_issue_ref("bad-ref")


def test_build_pr_body_rejects_linkage_keyword_in_notes() -> None:
    with pytest.raises(SystemExit) as exc:
        pr_body.build_pr_body(summary="s", links=[("Closes", "#42")], notes="Ref #99")
    assert "Ref #99" in str(exc.value)


def test_build_pr_body_rejects_linkage_keyword_in_summary() -> None:
    with pytest.raises(SystemExit) as exc:
        pr_body.build_pr_body(summary="Closes #7", links=[("Closes", "#42")], notes="")
    assert "Closes #7" in str(exc.value)


def test_build_pr_body_allows_bare_reference_in_notes() -> None:
    body = pr_body.build_pr_body(
        summary="s", links=[("Closes", "#42")], notes="See #99 for background"
    )
    assert "See #99 for background" in body


def test_build_pr_body_single_link_unchanged() -> None:
    """A one-issue body is byte-for-byte the single-issue body (Review focus 5)."""
    body = pr_body.build_pr_body(summary="Fix it", links=[("Closes", "#42")], notes="Tested")
    assert body == (
        "# Pull Request\n\n"
        "## Summary\n\n- Fix it\n\n"
        "## Issue Linkage\n\n- Closes #42\n\n"
        "## Notes\n\n- Tested"
    )


def test_build_pr_body_one_line_per_issue() -> None:
    links = [("Closes", "#10"), ("Closes", "#11"), ("Closes", "#12")]
    body = pr_body.build_pr_body(summary="s", links=links, notes="")
    assert "## Issue Linkage\n\n- Closes #10\n- Closes #11\n- Closes #12\n\n## Notes" in body
    # vrg-finalize-pr reads the body back with extract_tracking_refs.
    assert extract_tracking_refs(body) == links


def test_build_pr_body_refuses_no_links() -> None:
    with pytest.raises(SystemExit, match="at least one issue linkage"):
        pr_body.build_pr_body(summary="s", links=[], notes="")
