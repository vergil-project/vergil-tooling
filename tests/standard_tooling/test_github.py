"""Tests for standard_tooling.lib.github."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from standard_tooling.lib import github


def _completed(
    returncode: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_run_delegates_to_subprocess() -> None:
    with patch("standard_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed()
        github.run("pr", "list")
    mock_run.assert_called_once_with(("gh", "pr", "list"), check=True)


def test_read_output_returns_stripped_stdout() -> None:
    with patch("standard_tooling.lib.github.subprocess.run") as mock_run:
        mock_run.return_value = _completed(stdout="  result\n")
        assert github.read_output("pr", "view") == "result"
    mock_run.assert_called_once_with(
        ("gh", "pr", "view"), check=True, text=True, capture_output=True
    )


def test_create_pr_returns_url() -> None:
    with patch("standard_tooling.lib.github.read_output", return_value="https://github.com/pr/1"):
        url = github.create_pr(base="main", title="title", body_file="body.md")
    assert url == "https://github.com/pr/1"


def test_wait_for_checks_skips_poll_when_already_registered() -> None:
    with (
        patch("standard_tooling.lib.github._checks_registered", return_value=True),
        patch("standard_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1")
    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch", "--fail-fast"
    )


def test_wait_for_checks_polls_until_registered() -> None:
    with (
        patch(
            "standard_tooling.lib.github._checks_registered",
            side_effect=[False, False, True],
        ),
        patch("standard_tooling.lib.github.time.sleep") as mock_sleep,
        patch("standard_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)

    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(5)
    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch", "--fail-fast"
    )


def test_wait_for_checks_proceeds_after_timeout() -> None:
    # monotonic: [initial (deadline), loop iter1 check, loop iter2 check (expired)]
    with (
        patch("standard_tooling.lib.github._checks_registered", return_value=False),
        patch(
            "standard_tooling.lib.github.time.monotonic",
            side_effect=[0.0, 0.0, 61.0],
        ),
        patch("standard_tooling.lib.github.time.sleep"),
        patch("standard_tooling.lib.github.run") as mock_run,
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=5, poll_timeout=60)

    mock_run.assert_called_once_with(
        "pr", "checks", "https://github.com/pr/1", "--watch", "--fail-fast"
    )


def test_wait_for_checks_uses_poll_interval_for_sleep() -> None:
    with (
        patch(
            "standard_tooling.lib.github._checks_registered",
            side_effect=[False, True],
        ),
        patch("standard_tooling.lib.github.time.sleep") as mock_sleep,
        patch("standard_tooling.lib.github.run"),
    ):
        github.wait_for_checks("https://github.com/pr/1", poll_interval=10, poll_timeout=60)

    mock_sleep.assert_called_once_with(10)


def test_merge_delegates_to_gh() -> None:
    with patch("standard_tooling.lib.github.run") as mock_run:
        github.merge("https://github.com/pr/1", strategy="merge")
    mock_run.assert_called_once_with("pr", "merge", "--merge", "https://github.com/pr/1")


def test_merge_squash_strategy() -> None:
    with patch("standard_tooling.lib.github.run") as mock_run:
        github.merge("https://github.com/pr/1", strategy="squash")
    mock_run.assert_called_once_with("pr", "merge", "--squash", "https://github.com/pr/1")


def test_list_project_repos() -> None:
    with patch(
        "standard_tooling.lib.github.read_output",
        return_value="acme/repo-b\nacme/repo-a\nacme/repo-a\n",
    ):
        repos = github.list_project_repos("acme", "5")
    assert repos == ["acme/repo-a", "acme/repo-b"]


def test_list_project_repos_empty() -> None:
    with patch(
        "standard_tooling.lib.github.read_output",
        return_value="",
    ):
        assert github.list_project_repos("acme", "5") == []


def test_read_json_returns_parsed_dict() -> None:
    payload = {"name": "test", "value": 42}
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r")
    assert result == payload


def test_read_json_returns_parsed_list() -> None:
    payload = [{"id": 1}, {"id": 2}]
    cp = _completed(stdout=json.dumps(payload) + "\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        result = github.read_json("api", "repos/o/r/rulesets")
    assert result == payload


def test_checks_registered_returns_false_when_phrase_in_stdout() -> None:
    cp = _completed(returncode=1, stdout="no checks reported on the 'main' branch\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is False


def test_checks_registered_returns_false_when_phrase_in_stderr() -> None:
    cp = _completed(returncode=1, stderr="no checks reported on the 'main' branch\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is False


def test_checks_registered_returns_true_when_checks_exist() -> None:
    cp = _completed(stdout="ci/tests\tpass\nhttps://example.com\n")
    with patch("standard_tooling.lib.github.subprocess.run", return_value=cp):
        assert github._checks_registered("https://github.com/pr/1") is True
