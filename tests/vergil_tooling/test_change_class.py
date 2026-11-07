"""Tests for the merge-review change classifier and ``vrg-change-class``."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_change_class
from vergil_tooling.lib import git
from vergil_tooling.lib.merge_review.change_class import (
    BUILTIN_DOCS_PATHS,
    allow_list,
    classify,
    is_docs_path,
)
from vergil_tooling.lib.merge_review.schema import CHANGE_CLASSES

if TYPE_CHECKING:
    from pathlib import Path

Change = tuple[str, str, str | None]

_VERGIL_TOML = """\
[project]
repository-type = "library"
versioning-scheme = "semver"
branching-model = "library-release"
release-model = "tagged-release"

[dependencies]
vergil = "v2.0"

[ci]
versions = ["3.14"]
"""


# -- classify -------------------------------------------------------------------


def test_added_and_modified_docs_are_docs_only() -> None:
    changes = [("A", "README.md", None), ("M", "docs/guide/x.md", None)]
    assert classify(changes, ["README.md", "docs/guide/"]) == "docs-only"


def test_deletion_is_code() -> None:
    assert classify([("D", "docs/guide/x.md", None)], ["docs/guide/"]) == "code"


def test_rename_inside_docs_is_code() -> None:
    changes: list[Change] = [("R100", "docs/guide/a.md", "docs/guide/b.md")]
    assert classify(changes, ["docs/guide/"]) == "code"


def test_copy_inside_docs_is_code() -> None:
    changes: list[Change] = [("C075", "docs/guide/a.md", "docs/guide/b.md")]
    assert classify(changes, ["docs/guide/"]) == "code"


def test_path_off_list_is_code() -> None:
    assert classify([("M", "skills/x/SKILL.md", None)], ["docs/"]) == "code"


def test_one_path_off_list_makes_the_whole_change_code() -> None:
    changes = [("M", "docs/a.md", None), ("M", "src/a.py", None)]
    assert classify(changes, ["docs/"]) == "code"


def test_prefix_needs_slash() -> None:
    assert classify([("M", "docs-extra/x.md", None)], ["docs"]) == "code"


def test_exact_entry_matches_only_that_file() -> None:
    assert classify([("M", "docs/x.md", None)], ["docs"]) == "code"
    assert classify([("M", "NOTICE.md", None)], ["NOTICE.md"]) == "docs-only"


def test_vergil_toml_change_is_code() -> None:
    # A branch cannot widen its own allow-list.
    assert classify([("M", "vergil.toml", None)], ["vergil.toml", "src/"]) == "code"


def test_vergil_toml_with_widened_list_is_code() -> None:
    changes = [("M", "vergil.toml", None), ("A", "src/x.py", None)]
    assert classify(changes, [*BUILTIN_DOCS_PATHS, "vergil.toml", "src/"]) == "code"


@pytest.mark.parametrize("status", ["T", "U", "X", "D", ""])
def test_mode_or_type_change_is_code(status: str) -> None:
    assert classify([(status, "docs/x.md", None)], ["docs/"]) == "code"


def test_empty_change_is_code() -> None:
    assert classify([], ["docs/"]) == "code"


def test_empty_list_is_code() -> None:
    assert classify([("M", "docs/x.md", None)], []) == "code"


def test_classes_are_the_protocol_values() -> None:
    assert {classify([], []), classify([("A", "README.md", None)], ["README.md"])} == set(
        CHANGE_CLASSES
    )


def test_is_docs_path() -> None:
    assert is_docs_path("docs/a/b.md", ["docs/"])
    assert not is_docs_path("docs", ["docs/"])
    assert not is_docs_path("x/docs/a.md", ["docs/"])


def test_allow_list_starts_with_the_builtins() -> None:
    assert BUILTIN_DOCS_PATHS == ("README.md", "CHANGELOG.md")
    assert allow_list(("docs/guide/",)) == ("README.md", "CHANGELOG.md", "docs/guide/")
    assert allow_list(()) == BUILTIN_DOCS_PATHS


# -- a real repository ------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write(repo: Path, path: str, text: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


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
    _write(path, "vergil.toml", _VERGIL_TOML + '\n[merge-review]\ndocs-paths = ["docs/guide/"]\n')
    _write(path, "README.md", "# r\n")
    _write(path, "docs/guide/a.md", "a\n")
    _write(path, "src/x.py", "x = 1\n")
    _commit(path, "base")
    _git(path, "checkout", "-q", "-b", "feature")
    monkeypatch.chdir(path)
    return path


def test_base_moved_after_cut(repo: Path) -> None:
    # A code commit on the base after the branch was cut is not the branch's change.
    _write(repo, "docs/guide/b.md", "b\n")
    _commit(repo, "docs")
    _git(repo, "checkout", "-q", "develop")
    _write(repo, "src/x.py", "x = 2\n")
    _commit(repo, "code on base")
    _git(repo, "checkout", "-q", "feature")
    changes = git.diff_name_status("develop", "HEAD")
    assert changes == [("A", "docs/guide/b.md", None)]
    assert classify(changes, allow_list(["docs/guide/"])) == "docs-only"


def test_diff_name_status_reports_renames_and_deletions(repo: Path) -> None:
    _git(repo, "mv", "docs/guide/a.md", "docs/guide/renamed.md")
    _git(repo, "rm", "-q", "README.md")
    _write(repo, "src/x.py", "x = 3\n")
    _commit(repo, "mixed")
    changes = sorted(git.diff_name_status("develop", "feature"))
    assert changes == [
        ("D", "README.md", None),
        ("M", "src/x.py", None),
        ("R100", "docs/guide/a.md", "docs/guide/renamed.md"),
    ]


def test_diff_name_status_keeps_unusual_paths(repo: Path) -> None:
    _write(repo, "docs/guide/sp ace\tand-tab .md", "x\n")
    _commit(repo, "odd name")
    assert git.diff_name_status("develop", "HEAD") == [
        ("A", "docs/guide/sp ace\tand-tab .md", None)
    ]


def test_diff_name_status_empty(repo: Path) -> None:
    assert git.diff_name_status("develop", "HEAD") == []


def test_merge_base(repo: Path) -> None:
    cut = _git(repo, "rev-parse", "HEAD")
    _write(repo, "docs/guide/b.md", "b\n")
    _commit(repo, "docs")
    _git(repo, "checkout", "-q", "develop")
    _write(repo, "src/x.py", "x = 2\n")
    _commit(repo, "code on base")
    assert git.merge_base("develop", "feature") == cut


@pytest.mark.parametrize(
    ("base", "head"), [("--output=x", "HEAD"), ("develop", "-p"), ("", "HEAD")]
)
def test_option_like_revisions_are_refused(base: str, head: str) -> None:
    with pytest.raises(ValueError, match="not a git revision"):
        git.diff_name_status(base, head)
    with pytest.raises(ValueError, match="not a git revision"):
        git.merge_base(base, head)


def test_diff_name_status_refuses_a_truncated_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(git, "read_output", lambda *_args: "R100\0only-one-path\0")
    with pytest.raises(ValueError, match="truncated R100 entry"):
        git.diff_name_status("develop", "HEAD")


# -- vrg-change-class ---------------------------------------------------------------


def test_cli_docs_only(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, "docs/guide/a.md", "changed\n")
    _write(repo, "CHANGELOG.md", "# changes\n")
    _commit(repo, "docs")
    assert vrg_change_class.main(["--base", "develop"]) == 0
    assert capsys.readouterr().out == "docs-only\n"


def test_cli_code(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, "src/x.py", "x = 9\n")
    _commit(repo, "code")
    assert vrg_change_class.main(["--base", "develop", "--head", "feature"]) == 0
    assert capsys.readouterr().out == "code\n"


def test_cli_branch_widening_its_own_list_is_code(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    widened = '\n[merge-review]\ndocs-paths = ["vergil.toml", "src/"]\n'
    _write(repo, "vergil.toml", _VERGIL_TOML + widened)
    _write(repo, "src/x.py", "x = 9\n")
    _commit(repo, "widen")
    assert vrg_change_class.main(["--base", "develop"]) == 0
    assert capsys.readouterr().out == "code\n"


def test_cli_without_repo_paths_uses_builtins(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _git(repo, "checkout", "-q", "develop")
    _write(repo, "vergil.toml", _VERGIL_TOML)
    _commit(repo, "no merge-review section")
    _git(repo, "checkout", "-q", "-b", "feature2")
    _write(repo, "docs/guide/a.md", "changed\n")
    _commit(repo, "guide")
    assert vrg_change_class.main(["--base", "develop"]) == 0
    assert capsys.readouterr().out == "code\n"


def test_cli_unknown_ref(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_change_class.main(["--base", "no-such-ref"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR: git failed" in captured.err


def test_cli_option_like_ref(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert vrg_change_class.main(["--base=--output=x"]) == 1
    assert "ERROR: not a git revision" in capsys.readouterr().err
    assert not (repo / "x").exists()


def test_cli_invalid_config(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(repo, "vergil.toml", _VERGIL_TOML + '\n[merge-review]\ndocs-paths = "docs/"\n')
    assert vrg_change_class.main(["--base", "develop"]) == 1
    assert "docs-paths must be a list of strings" in capsys.readouterr().err


def test_cli_missing_config(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "vergil.toml").unlink()
    assert vrg_change_class.main(["--base", "develop"]) == 1
    assert "vergil.toml not found" in capsys.readouterr().err


def test_cli_requires_base() -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_change_class.main([])
    assert exc.value.code == 2
