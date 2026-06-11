from __future__ import annotations

import pytest

from vergil_tooling.lib.release import checklist


def test_markers_are_html_comments() -> None:
    assert checklist.BEGIN == "<!-- vrg-release:progress -->"
    assert checklist.END == "<!-- /vrg-release:progress -->"


def test_render_unchecked_and_checked() -> None:
    block = checklist.render(["audit", "prepare"], checked={"audit"})
    assert block == (
        "<!-- vrg-release:progress -->\n"
        "- [x] audit\n"
        "- [ ] prepare\n"
        "<!-- /vrg-release:progress -->"
    )


def test_render_empty_checked_defaults_to_all_unchecked() -> None:
    block = checklist.render(["audit"])
    assert "- [ ] audit" in block


def test_parse_returns_stage_state_pairs() -> None:
    body = (
        "## Release 2.1.0\n\n"
        + checklist.render(["audit", "prepare"], checked={"audit"})
        + "\n\nmore text\n"
    )
    assert checklist.parse(body) == [("audit", True), ("prepare", False)]


def test_parse_accepts_capital_x() -> None:
    body = checklist.render(["audit"]).replace("[ ] audit", "[X] audit")
    assert checklist.parse(body) == [("audit", True)]


def test_parse_ignores_non_item_lines_in_block() -> None:
    body = (
        checklist.BEGIN
        + "\nsome note\n- [ ] audit\n"
        + checklist.END
    )
    assert checklist.parse(body) == [("audit", False)]


def test_parse_raises_when_no_block() -> None:
    with pytest.raises(checklist.ChecklistError, match="no .* progress block"):
        checklist.parse("## Release 2.1.0\n")


def test_upsert_appends_when_absent() -> None:
    body = checklist.upsert("## Release 2.1.0\n", ["audit"])
    assert "## Release 2.1.0" in body
    assert checklist.parse(body) == [("audit", False)]


def test_upsert_replaces_existing_block_preserving_surroundings() -> None:
    original = "head\n\n" + checklist.render(["audit"]) + "\n\ntail\n"
    updated = checklist.upsert(original, ["audit"], checked={"audit"})
    assert updated.startswith("head")
    assert updated.rstrip().endswith("tail")
    assert checklist.parse(updated) == [("audit", True)]
