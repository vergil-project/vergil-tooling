"""Tests for vergil_tooling.bin.vrg_pr_review_comments."""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from vergil_tooling.bin.vrg_pr_review_comments import (
    MalformedCommentError,
    ReviewComment,
    format_text,
    main,
    parse_args,
    parse_comment,
)
from vergil_tooling.lib import github

if TYPE_CHECKING:
    from collections.abc import Iterator

_MOD = "vergil_tooling.bin.vrg_pr_review_comments"


def _api_comment(**overrides: object) -> dict[str, object]:
    comment: dict[str, object] = {
        "id": 11,
        "user": {"login": "reviewer"},
        "path": "src/app.py",
        "line": 42,
        "original_line": 40,
        "pull_request_review_id": 7,
        "created_at": "2026-11-01T12:00:00Z",
        "body": "Consider a guard here.",
        "html_url": "https://github.com/o/r/pull/5#discussion_r11",
        "diff_hunk": "@@ -1 +1 @@",
    }
    comment.update(overrides)
    return comment


@pytest.fixture
def paginated() -> Iterator[MagicMock]:
    with patch(f"{_MOD}.github.read_json_paginated") as mock:
        yield mock


# --- parse_args ---


def test_parse_args() -> None:
    args = parse_args(["--repo", "vergil-project/vergil-tooling", "--pr", "12"])
    assert args.repo == "vergil-project/vergil-tooling"
    assert args.pr == 12
    assert args.json is False


def test_parse_args_json() -> None:
    assert parse_args(["--repo", "o/r", "--pr", "1", "--json"]).json is True


@pytest.mark.parametrize(
    "repo",
    ["o", "o/r/x", "/r", "o/", "o/..", "o/.", "-o/r", "o r/x", "o/r?x=1", "../r", "o/r#1"],
)
def test_parse_args_rejects_bad_repo(repo: str) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["--repo", repo, "--pr", "1"])
    assert exc.value.code == 2


@pytest.mark.parametrize("pr", ["0", "-1", "abc", "1.5", "²", " 3", "3?x=1"])
def test_parse_args_rejects_bad_pr(pr: str) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_args(["--repo", "o/r", "--pr", pr])
    assert exc.value.code == 2


@pytest.mark.parametrize("missing", ["--repo", "--pr"])
def test_parse_args_requires_repo_and_pr(missing: str) -> None:
    argv = {"--repo": "o/r", "--pr": "1"}
    del argv[missing]
    with pytest.raises(SystemExit) as exc:
        parse_args([part for pair in argv.items() for part in pair])
    assert exc.value.code == 2


# --- parse_comment ---


def test_parse_comment_projects_fields() -> None:
    comment = parse_comment(_api_comment(in_reply_to_id=10), 0)
    assert comment == ReviewComment(
        id=11,
        author="reviewer",
        path="src/app.py",
        line=42,
        review_id=7,
        in_reply_to_id=10,
        created_at="2026-11-01T12:00:00Z",
        body="Consider a guard here.",
        url="https://github.com/o/r/pull/5#discussion_r11",
    )


def test_parse_comment_nullable_fields() -> None:
    raw = _api_comment(user=None, line=None, pull_request_review_id=None)
    comment = parse_comment(raw, 0)
    assert comment.author is None
    assert comment.line is None
    assert comment.review_id is None
    assert comment.in_reply_to_id is None


@pytest.mark.parametrize("field", ["id", "path", "created_at", "body", "html_url"])
def test_parse_comment_missing_required_field(field: str) -> None:
    raw = _api_comment()
    del raw[field]
    with pytest.raises(MalformedCommentError, match=f"item 3 field '{field}' is missing"):
        parse_comment(raw, 3)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "11"),
        ("id", True),
        ("path", 5),
        ("line", "42"),
        ("line", False),
        ("pull_request_review_id", "7"),
        ("in_reply_to_id", 1.5),
        ("body", None),
    ],
)
def test_parse_comment_wrong_type(field: str, value: object) -> None:
    with pytest.raises(MalformedCommentError, match=f"field '{field}'"):
        parse_comment(_api_comment(**{field: value}), 0)


def test_parse_comment_not_an_object() -> None:
    with pytest.raises(MalformedCommentError, match="item 2 is not an object: list"):
        parse_comment([], 2)


def test_parse_comment_bad_user() -> None:
    with pytest.raises(MalformedCommentError, match="'user' is not an object"):
        parse_comment(_api_comment(user="reviewer"), 0)
    with pytest.raises(MalformedCommentError, match="user field 'login' is missing"):
        parse_comment(_api_comment(user={}), 0)


# --- main ---


def test_main_json_reads_every_page_of_the_endpoint(
    paginated: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    paginated.return_value = [_api_comment(), _api_comment(id=12, in_reply_to_id=11, line=None)]
    assert main(["--repo", "o/r", "--pr", "5", "--json"]) == 0
    paginated.assert_called_once_with("repos/o/r/pulls/5/comments")
    out = json.loads(capsys.readouterr().out)
    assert [c["id"] for c in out] == [11, 12]
    assert out[1]["in_reply_to_id"] == 11
    assert out[1]["line"] is None
    assert set(out[0]) == {
        "id",
        "author",
        "path",
        "line",
        "review_id",
        "in_reply_to_id",
        "created_at",
        "body",
        "url",
    }


def test_main_scopes_the_token_to_the_repo_owner(paginated: MagicMock) -> None:
    seen: list[str | None] = []

    def record(_endpoint: str) -> list[object]:
        seen.append(github._target_org.get())  # noqa: SLF001
        return []

    paginated.side_effect = record
    assert main(["--repo", "other-org/r", "--pr", "5"]) == 0
    assert seen == ["other-org"]


def test_main_json_empty(paginated: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    paginated.return_value = []
    assert main(["--repo", "o/r", "--pr", "5", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_main_text(paginated: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    paginated.return_value = [_api_comment()]
    assert main(["--repo", "o/r", "--pr", "5"]) == 0
    out = capsys.readouterr().out
    assert "src/app.py:42 by reviewer at 2026-11-01T12:00:00Z (comment 11, review 7)" in out
    assert "    Consider a guard here." in out


def test_main_api_error_exits_1(paginated: MagicMock, capsys: pytest.CaptureFixture[str]) -> None:
    paginated.side_effect = github.GitHubAPIError(1, ["gh", "api"], "", "HTTP 404: Not Found")
    assert main(["--repo", "o/r", "--pr", "5", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR: failed to read the review comments of o/r#5" in captured.err
    assert "HTTP 404" in captured.err


def test_main_bare_process_error_exits_1(
    paginated: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    paginated.side_effect = subprocess.CalledProcessError(1, ["gh"])
    assert main(["--repo", "o/r", "--pr", "5"]) == 1
    assert "ERROR: failed to read" in capsys.readouterr().err


def test_main_malformed_page_exits_1(
    paginated: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    paginated.side_effect = ValueError("paginated response is not a list: dict")
    assert main(["--repo", "o/r", "--pr", "5", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR: malformed review comments response for o/r#5" in captured.err


def test_main_malformed_comment_exits_1(
    paginated: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    paginated.return_value = [_api_comment(), {"id": 2}]
    assert main(["--repo", "o/r", "--pr", "5", "--json"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "item 1 field" in captured.err


def test_main_missing_token_is_the_auth_message(
    paginated: MagicMock, capsys: pytest.CaptureFixture[str]
) -> None:
    paginated.side_effect = github.MissingGitHubTokenError(1, ["gh"], "", "no token")
    with pytest.raises(SystemExit) as exc:
        main(["--repo", "o/r", "--pr", "5"])
    assert exc.value.code == 1
    assert "no GitHub token available" in capsys.readouterr().err


# --- format_text ---


def _comment(**overrides: object) -> ReviewComment:
    return parse_comment(_api_comment(**overrides), 0)


def test_format_text_empty() -> None:
    assert format_text("o/r", 5, []) == "o/r#5 has no inline review comments."


def test_format_text_outdated_reply_from_deleted_user() -> None:
    text = format_text(
        "o/r",
        5,
        [_comment(user=None, line=None, pull_request_review_id=None, in_reply_to_id=9)],
    )
    assert text.splitlines()[0] == (
        "src/app.py:outdated by (deleted user) at 2026-11-01T12:00:00Z (comment 11, reply to 9)"
    )


def test_format_text_indents_multiline_body_and_separates_blocks() -> None:
    text = format_text("o/r", 5, [_comment(body="one\ntwo"), _comment(id=12, body="")])
    blocks = text.split("\n\n")
    assert len(blocks) == 2
    assert blocks[0].splitlines()[2:] == ["    one", "    two"]
    assert blocks[1].splitlines()[2] == "    (empty)"
