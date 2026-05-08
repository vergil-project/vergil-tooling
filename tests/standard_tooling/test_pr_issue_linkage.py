"""Tests for standard_tooling.bin.pr_issue_linkage."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from standard_tooling.bin.pr_issue_linkage import main

if TYPE_CHECKING:
    from pathlib import Path


def _write_event(tmp_path: Path, body: str) -> str:
    event = {"pull_request": {"body": body}}
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps(event))
    return str(event_file)


def test_missing_env_var() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert main() == 2


def test_missing_event_file() -> None:
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": "/nonexistent/event.json"}):
        assert main() == 2


def test_empty_body(tmp_path: Path) -> None:
    event_path = _write_event(tmp_path, "")
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 1


def test_null_body(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"pull_request": {"body": None}}))
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": str(event_file)}):
        assert main() == 1


def test_no_linkage(tmp_path: Path) -> None:
    event_path = _write_event(tmp_path, "This PR does something nice.")
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 1


def test_ref_linkage(tmp_path: Path) -> None:
    event_path = _write_event(tmp_path, "Ref #123\n")
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 0


def test_ref_cross_repo_linkage(tmp_path: Path) -> None:
    event_path = _write_event(tmp_path, "Ref owner/repo#123\n")
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 0


def test_ref_bullet_linkage(tmp_path: Path) -> None:
    event_path = _write_event(tmp_path, "- Ref #42\n")
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 0


def test_ref_star_bullet_linkage(tmp_path: Path) -> None:
    event_path = _write_event(tmp_path, "* Ref #42\n")
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 0


def test_ref_with_colon(tmp_path: Path) -> None:
    event_path = _write_event(tmp_path, "Ref: #42\n")
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 0


def test_ref_indented(tmp_path: Path) -> None:
    event_path = _write_event(tmp_path, "  Ref #42\n")
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 0


# -- auto-close keyword rejection -------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "Fixes #42",
        "Closes #99",
        "Resolves #7",
        "closes #42",
        "FIXES #42",
        "Fixed #42",
        "Close #42",
        "Resolved #42",
        "- Fixes #42",
        "* Closes #42",
        "  Resolves #42",
        "Fixes: #42",
        "Fixes owner/repo#123",
    ],
)
def test_rejects_autoclose_keywords(tmp_path: Path, body: str) -> None:
    event_path = _write_event(tmp_path, body)
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": event_path}):
        assert main() == 1


def test_no_pull_request_key(tmp_path: Path) -> None:
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({}))
    with patch.dict("os.environ", {"GITHUB_EVENT_PATH": str(event_file)}):
        assert main() == 1
