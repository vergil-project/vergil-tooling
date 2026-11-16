"""Tests for the per-update package change list (vergil-tooling#4295)."""

from __future__ import annotations

from vergil_tooling.lib.update_deps import changes
from vergil_tooling.lib.update_deps.changes import PackageChange
from vergil_tooling.lib.update_deps.updater import UpdateResult


def _result(updater: str, packages: list[PackageChange] | None, *, changed: bool = True):
    return UpdateResult(
        updater=updater, changed=changed, summary="s", commit_message="m", packages=packages
    )


def test_diff_versions_upgrades_additions_and_removals_by_name() -> None:
    before = {"b": ["1.0"], "a": ["2.0"], "same": ["1"], "gone": ["0.1"]}
    after = {"b": ["1.1"], "a": ["2.0"], "same": ["1"], "new": ["3"]}
    assert changes.diff_versions(before, after) == [
        PackageChange("b", "1.0", "1.1"),
        PackageChange("gone", "0.1", None),
        PackageChange("new", None, "3"),
    ]


def test_diff_versions_compares_forked_versions_as_a_set() -> None:
    assert changes.diff_versions({"x": ["2", "1"]}, {"x": ["1", "2"]}) == []
    assert changes.diff_versions({"x": ["1", "2"]}, {"x": ["1", "3"]}) == [
        PackageChange("x", "1, 2", "1, 3")
    ]


def test_render_counts_each_changed_updater_and_lists_packages() -> None:
    text = changes.render_markdown(
        [
            _result("python", [PackageChange("a", "1", "2"), PackageChange("b", None, "1")]),
            _result("images", [PackageChange("x|y", "1.0.0", "1.0.1")]),
            _result("vergil", []),
            _result("hook", None),
            _result("skipped", [PackageChange("z", "1", "2")], changed=False),
        ]
    )
    assert text == "\n".join(
        [
            "## Package changes",
            "",
            "- **python** — 2 packages",
            "- **images** — 1 package",
            "- **vergil** — no package versions changed",
            "- **hook** — not itemized",
            "",
            "| Updater | Package | From | To |",
            "|---|---|---|---|",
            "| python | a | 1 | 2 |",
            "| python | b | (new) | 1 |",
            "| images | x\\|y | 1.0.0 | 1.0.1 |",
        ]
    )


def test_render_shows_removals_and_omits_the_table_with_no_rows() -> None:
    assert "| python | a | 1 | (removed) |" in changes.render_markdown(
        [_result("python", [PackageChange("a", "1", None)])]
    )
    assert changes.render_markdown([_result("hook", None)]) == (
        "## Package changes\n\n- **hook** — not itemized"
    )


def test_render_caps_rows() -> None:
    many = [PackageChange(f"p{i}", "1", "2") for i in range(5)]
    text = changes.render_markdown([_result("python", many)], max_rows=3)
    assert "- **python** — 5 packages" in text
    assert "| python | p2 | 1 | 2 |" in text
    assert "p3" not in text
    assert text.endswith("…and 2 more.")
