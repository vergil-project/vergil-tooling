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
