from __future__ import annotations

import pytest

from vergil_tooling.lib.release import checklist


def test_markers_are_html_comments() -> None:
    assert checklist.BEGIN == "<!-- vrg-release:progress -->"
    assert checklist.END == "<!-- /vrg-release:progress -->"
