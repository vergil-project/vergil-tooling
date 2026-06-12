from __future__ import annotations

from unittest.mock import patch

import pytest

from vergil_tooling.lib.release import checklist
from vergil_tooling.lib.release.context import ReleaseError
from vergil_tooling.lib.release.resume import _open_release_issues, find_resume_target

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
