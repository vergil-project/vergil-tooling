"""Tests for the rebase-stable change fingerprint (``lib.change_fingerprint``)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.change_fingerprint import (
    FingerprintError,
    change_fingerprint,
    commit_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_TWENTY = "".join(f"line {i}\n" for i in range(1, 21))


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repo: Path, path: str, data: str | bytes) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        target.write_bytes(data)
    else:
        target.write_text(data)


def _commit(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _replace_line(repo: Path, path: str, number: int, text: str) -> None:
    lines = (repo / path).read_text().splitlines(keepends=True)
    lines[number - 1] = text
    (repo / path).write_text("".join(lines))


def _on_develop(repo: Path, message: str, edit: Callable[[], None]) -> None:
    _git(repo, "checkout", "-q", "develop")
    edit()
    _commit(repo, message)
    _git(repo, "checkout", "-q", "feature")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo on ``develop`` with one commit, and a ``feature`` branch checked out."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    _write(path, "a.txt", _TWENTY)
    _write(path, "img.bin", b"\x00\x01\x02binary\x00")
    _commit(path, "base")
    _git(path, "checkout", "-q", "-b", "feature")
    return path


def test_fingerprint_survives_nearby_develop_edit(repo: Path) -> None:
    _replace_line(repo, "a.txt", 10, "branch line 10\n")
    _commit(repo, "branch")
    before = change_fingerprint(repo, "develop", "feature")
    _on_develop(repo, "near", lambda: _replace_line(repo, "a.txt", 8, "develop line 8\n"))
    _git(repo, "rebase", "-q", "develop")
    assert change_fingerprint(repo, "develop", "feature") == before


def test_fingerprint_survives_develop_edit_elsewhere_in_file(repo: Path) -> None:
    _replace_line(repo, "a.txt", 3, "branch line 3\n")
    _commit(repo, "branch")
    before = change_fingerprint(repo, "develop", "feature")
    _on_develop(repo, "far", lambda: _replace_line(repo, "a.txt", 18, "develop line 18\n"))
    _git(repo, "rebase", "-q", "develop")
    assert change_fingerprint(repo, "develop", "feature") == before


def test_conflict_resolution_changes_fingerprint(repo: Path) -> None:
    _replace_line(repo, "a.txt", 10, "branch line 10\n")
    _commit(repo, "branch")
    before = change_fingerprint(repo, "develop", "feature")
    _on_develop(repo, "same", lambda: _replace_line(repo, "a.txt", 10, "develop line 10\n"))
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "rebase", "-q", "develop"],
        cwd=repo,
        check=False,
        capture_output=True,
    )
    assert result.returncode != 0
    content = (repo / "a.txt").read_text()
    start = content.index("<<<<<<<")
    end = content.index(">>>>>>>")
    end = content.index("\n", end) + 1
    (repo / "a.txt").write_text(content[:start] + "resolved line 10\n" + content[end:])
    _git(repo, "add", "a.txt")
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.editor=true", "rebase", "--continue"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    assert change_fingerprint(repo, "develop", "feature") != before


def test_fingerprint_ignores_line_shift(repo: Path) -> None:
    _replace_line(repo, "a.txt", 12, "branch line 12\n")
    _commit(repo, "branch")
    before = change_fingerprint(repo, "develop", "feature")

    def insert_at_top() -> None:
        _write(repo, "a.txt", "new 1\nnew 2\nnew 3\n" + (repo / "a.txt").read_text())

    _on_develop(repo, "shift", insert_at_top)
    _git(repo, "rebase", "-q", "develop")
    assert change_fingerprint(repo, "develop", "feature") == before


def test_whitespace_only_change_changes_fingerprint(repo: Path) -> None:
    _replace_line(repo, "a.txt", 5, "line  5\n")
    _commit(repo, "branch")
    spaced = change_fingerprint(repo, "develop", "feature")
    _replace_line(repo, "a.txt", 5, "line\t5\n")
    _git(repo, "commit", "-q", "--amend", "-a", "--no-edit")
    tabbed = change_fingerprint(repo, "develop", "feature")
    _replace_line(repo, "a.txt", 5, "line 5 \n")
    _git(repo, "commit", "-q", "--amend", "-a", "--no-edit")
    trailing = change_fingerprint(repo, "develop", "feature")
    assert len({spaced, tabbed, trailing}) == 3


def test_binary_change_changes_fingerprint(repo: Path) -> None:
    _write(repo, "img.bin", b"\x00\x01\x02first\x00")
    _commit(repo, "branch")
    first = change_fingerprint(repo, "develop", "feature")
    _write(repo, "img.bin", b"\x00\x01\x02other\x00")
    _git(repo, "commit", "-q", "--amend", "-a", "--no-edit")
    second = change_fingerprint(repo, "develop", "feature")
    assert first != second


def test_binary_deletion_has_a_fingerprint(repo: Path) -> None:
    (repo / "img.bin").unlink()
    _commit(repo, "branch")
    assert len(change_fingerprint(repo, "develop", "feature")) == 64


def test_rename_survives_develop_edit_to_renamed_file(repo: Path) -> None:
    _git(repo, "mv", "a.txt", "b.txt")
    _replace_line(repo, "b.txt", 2, "branch line 2\n")
    _commit(repo, "branch")
    before = change_fingerprint(repo, "develop", "feature")
    _on_develop(repo, "edit", lambda: _replace_line(repo, "a.txt", 17, "develop line 17\n"))
    _git(repo, "rebase", "-q", "develop")
    assert (repo / "b.txt").read_text().count("develop line 17") == 1
    assert change_fingerprint(repo, "develop", "feature") == before


def test_empty_diff_raises(repo: Path) -> None:
    with pytest.raises(FingerprintError, match="empty"):
        change_fingerprint(repo, "develop", "feature")


def test_unknown_ref_raises(repo: Path) -> None:
    with pytest.raises(FingerprintError, match="no-such-ref"):
        change_fingerprint(repo, "develop", "no-such-ref")


def test_local_diff_config_does_not_change_fingerprint(repo: Path) -> None:
    _replace_line(repo, "a.txt", 10, "branch line 10\n")
    _write(repo, "c.txt", "c\n")
    _commit(repo, "branch")
    before = change_fingerprint(repo, "develop", "feature")
    for key, value in (
        ("diff.noprefix", "true"),
        ("diff.mnemonicPrefix", "true"),
        ("diff.algorithm", "patience"),
        ("diff.interHunkContext", "5"),
        ("diff.orderFile", str(repo / ".git" / "order")),
        ("color.diff", "always"),
        ("core.quotePath", "false"),
    ):
        _git(repo, "config", key, value)
    (repo / ".git" / "order").write_text("c.txt\na.txt\n")
    assert change_fingerprint(repo, "develop", "feature") == before


def test_commit_fingerprint_is_the_commits_own_diff(repo: Path) -> None:
    _replace_line(repo, "a.txt", 3, "branch line 3\n")
    first = _commit(repo, "first")
    alone = change_fingerprint(repo, "develop", "feature")
    _replace_line(repo, "a.txt", 15, "branch line 15\n")
    second = _commit(repo, "second")
    assert commit_fingerprint(repo, first) == alone
    assert commit_fingerprint(repo, second) != alone
    assert change_fingerprint(repo, "develop", "feature") not in {
        commit_fingerprint(repo, first),
        commit_fingerprint(repo, second),
    }


def test_commit_fingerprint_of_root_commit_raises(repo: Path) -> None:
    root = _git(repo, "rev-list", "--max-parents=0", "HEAD")
    with pytest.raises(FingerprintError):
        commit_fingerprint(repo, root)


def test_worktree_attributes_do_not_change_fingerprint(repo: Path) -> None:
    _replace_line(repo, "a.txt", 10, "branch line 10\n")
    _commit(repo, "branch")
    before = change_fingerprint(repo, "develop", "feature")
    _write(repo, ".gitattributes", "a.txt -diff\n")
    assert change_fingerprint(repo, "develop", "feature") == before


def test_diff_environment_does_not_change_fingerprint(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _replace_line(repo, "a.txt", 10, "branch line 10\n")
    _commit(repo, "branch")
    before = change_fingerprint(repo, "develop", "feature")
    monkeypatch.setenv("GIT_DIFF_OPTS", "--unified=5")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'diff.noprefix'='true'")
    assert change_fingerprint(repo, "develop", "feature") == before


def test_file_marked_binary_at_head_counts_by_blob(repo: Path) -> None:
    _write(repo, ".gitattributes", "a.txt -diff\n")
    _replace_line(repo, "a.txt", 10, "branch line 10\n")
    _commit(repo, "branch")
    first = change_fingerprint(repo, "develop", "feature")
    _replace_line(repo, "a.txt", 10, "branch line X\n")
    _git(repo, "commit", "-q", "--amend", "-a", "--no-edit")
    assert change_fingerprint(repo, "develop", "feature") != first
