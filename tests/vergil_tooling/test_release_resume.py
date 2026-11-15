from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib.release import checklist
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.resume import (
    _SEARCH_LIST_LIMIT,
    _open_release_issues,
    find_resume_target,
)

_MOD = "vergil_tooling.lib.release.resume"
_STAGES = ["audit", "prepare"]


def _issue(version: str, number: int, checked: tuple[str, ...] = ()) -> dict[str, object]:
    body = "## Release\n\n" + checklist.render(_STAGES, checked)
    return {"number": number, "title": f"release: {version}", "body": body}


def test_find_resume_target_single() -> None:
    with patch(_MOD + ".github.read_json", return_value=[_issue("2.1.0", 42)]):
        assert find_resume_target("o/r", _STAGES) == ("2.1.0", 42)


def test_find_resume_target_none_raises() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[]),
        pytest.raises(ReleaseError, match="No in-flight release"),
    ):
        find_resume_target("o/r", _STAGES)


def _limited(rows: list[dict[str, object]]):
    """A ``gh issue list`` stand-in that returns at most ``--limit`` rows (30 without one)."""

    def fake(*args: str) -> list[dict[str, object]]:
        return rows[: int(args[args.index("--limit") + 1]) if "--limit" in args else 30]

    return fake


def _noise(count: int) -> list[dict[str, object]]:
    # The search is a broad net: these match it but are not release issues.
    return [{"number": i, "title": f"release: notes {i}", "body": ""} for i in range(count)]


def test_find_resume_target_sees_a_release_issue_past_row_30() -> None:
    rows = [*_noise(30), _issue("2.1.0", 42)]
    with patch(_MOD + ".github.read_json", side_effect=_limited(rows)):
        assert find_resume_target("o/r", _STAGES) == ("2.1.0", 42)


def test_find_resume_target_refuses_a_search_that_may_be_truncated() -> None:
    rows = _noise(_SEARCH_LIST_LIMIT)
    with (
        patch(_MOD + ".github.read_json", side_effect=_limited(rows)),
        pytest.raises(ReleaseError, match="possibly truncated list"),
    ):
        find_resume_target("o/r", _STAGES)


def test_find_resume_target_multiple_raises() -> None:
    issues = [_issue("2.1.0", 42), _issue("2.2.0", 43)]
    with (
        patch(_MOD + ".github.read_json", return_value=issues),
        pytest.raises(ReleaseError, match="Multiple in-flight"),
    ):
        find_resume_target("o/r", _STAGES)


def test_find_resume_target_version_disambiguates() -> None:
    issues = [_issue("2.1.0", 42), _issue("2.2.0", 43)]
    with patch(_MOD + ".github.read_json", return_value=issues):
        assert find_resume_target("o/r", _STAGES, version="2.2.0") == ("2.2.0", 43)


def test_find_resume_target_version_not_found_raises() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[_issue("2.1.0", 42)]),
        pytest.raises(ReleaseError, match="No open release issue for 2.9.9"),
    ):
        find_resume_target("o/r", _STAGES, version="2.9.9")


def test_find_resume_target_skew_raises() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[_issue("2.1.0", 42)]),
        pytest.raises(ReleaseError, match="different .* version"),
    ):
        find_resume_target("o/r", ["audit", "merge"])


def _blockless(version: str, number: int) -> dict[str, object]:
    # An issue an older vrg-release opened and was interrupted before it wrote
    # the checklist (#4367).
    return {"number": number, "title": f"release: {version}", "body": "## Release\n"}


def test_find_resume_target_adopts_a_blockless_issue_with_a_local_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[_blockless("2.1.0", 42)]),
        patch(_MOD + ".git.ref_exists", side_effect=lambda ref: ref == "release/2.1.0"),
    ):
        assert find_resume_target("o/r", _STAGES) == ("2.1.0", 42)
    assert "no vrg-release progress block" in capsys.readouterr().out


def test_find_resume_target_adopts_a_blockless_issue_with_a_pushed_branch() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[_blockless("2.1.0", 42)]),
        patch(_MOD + ".git.ref_exists", side_effect=lambda ref: ref == "origin/release/2.1.0"),
    ):
        assert find_resume_target("o/r", _STAGES) == ("2.1.0", 42)


def test_find_resume_target_refuses_a_blockless_issue_with_no_branch() -> None:
    with (
        patch(_MOD + ".github.read_json", return_value=[_blockless("2.1.0", 42)]),
        patch(_MOD + ".git.ref_exists", return_value=False),
        pytest.raises(ReleaseError, match="Close the issue and re-run vrg-release"),
    ):
        find_resume_target("o/r", _STAGES)


def test_find_resume_target_refuses_a_half_written_block() -> None:
    issue = {"number": 42, "title": "release: 2.1.0", "body": checklist.BEGIN + "\n- [ ] audit"}
    with (
        patch(_MOD + ".github.read_json", return_value=[issue]),
        patch(_MOD + ".git.ref_exists") as m_ref,
        pytest.raises(ReleaseError, match="no vrg-release progress block found"),
    ):
        find_resume_target("o/r", _STAGES)
    m_ref.assert_not_called()


def test_open_release_issues_skips_non_dicts_and_non_release_titles() -> None:
    results = [
        "not a dict",
        {"number": 1, "title": "not a release", "body": ""},
        _issue("2.1.0", 42),
    ]
    with patch(_MOD + ".github.read_json", return_value=results):
        assert [(v, n) for v, n, _ in _open_release_issues("o/r")] == [("2.1.0", 42)]


def test_open_release_issues_non_list_returns_empty() -> None:
    with patch(_MOD + ".github.read_json", return_value={"error": "x"}):
        assert _open_release_issues("o/r") == []
