"""Tests for the protected-path set and its match (epic vergil-project/.github#1384, Task 4)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.protected_paths import (
    FLEET_DEFAULT,
    REGISTRY_FILE,
    ProtectedChange,
    ProtectedPathsError,
    changed_protected,
    load_registry,
    matches,
    parse_registry,
    protected_set,
)

if TYPE_CHECKING:
    from pathlib import Path


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


def _blob(repo: Path, ref: str, path: str) -> str:
    return _git(repo, "rev-parse", f"{ref}:{path}")


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
    _write(path, "vergil.toml", "[project]\n")
    _write(path, ".github/workflows/ci.yml", "on: push\n")
    _write(path, "actions/setup/action.yml", "runs: {}\n")
    _write(path, REGISTRY_FILE, 'paths = ["actions/**"]\n')
    _write(path, "src/x.py", "x = 1\n")
    _commit(path, "base")
    _git(path, "checkout", "-q", "-b", "feature")
    return path


# -- the set -----------------------------------------------------------------------


def test_fleet_default_is_the_core() -> None:
    assert FLEET_DEFAULT == (".github/workflows/**", "vergil.toml", "protected-paths.toml")
    assert REGISTRY_FILE == "protected-paths.toml"


def test_workflow_and_vergil_toml_are_protected_by_default(repo: Path) -> None:
    _write(repo, ".github/workflows/ci.yml", "on: pull_request\n")
    _write(repo, ".github/workflows/nested/x.yml", "on: push\n")
    _write(repo, "vergil.toml", "[project]\nname = 'r'\n")
    _commit(repo, "wiring")
    changes = changed_protected(repo, "develop", "feature", "develop")
    assert changes == [
        ProtectedChange(
            ".github/workflows/ci.yml", _blob(repo, "feature", ".github/workflows/ci.yml")
        ),
        ProtectedChange(
            ".github/workflows/nested/x.yml",
            _blob(repo, "feature", ".github/workflows/nested/x.yml"),
        ),
        ProtectedChange("vergil.toml", _blob(repo, "feature", "vergil.toml")),
    ]


def test_ordinary_file_is_not_protected(repo: Path) -> None:
    _write(repo, "src/x.py", "x = 2\n")
    _write(repo, "docs/vergil.toml.md", "about\n")
    _write(repo, "sub/vergil.toml", "[project]\n")
    _write(repo, ".github/workflows-notes.md", "notes\n")
    _commit(repo, "code")
    assert changed_protected(repo, "develop", "feature", "develop") == []


def test_protected_set_is_the_default_then_the_registry(repo: Path) -> None:
    assert protected_set(repo, "develop") == (*FLEET_DEFAULT, "actions/**")


def test_registry_is_read_from_develop_not_the_pr(repo: Path) -> None:
    _write(repo, REGISTRY_FILE, "paths = []\n")
    _write(repo, "actions/setup/action.yml", "runs: {changed: true}\n")
    _commit(repo, "drop actions from the registry")
    changes = changed_protected(repo, "develop", "feature", "develop")
    assert [change.path for change in changes] == ["actions/setup/action.yml", REGISTRY_FILE]
    assert changes[1].blob == _blob(repo, "feature", REGISTRY_FILE)


def test_registry_added_by_the_pr_does_not_apply(repo: Path) -> None:
    _write(repo, REGISTRY_FILE, 'paths = ["actions/**", "src/**"]\n')
    _commit(repo, "widen in the PR")
    _write(repo, "src/x.py", "x = 3\n")
    _commit(repo, "code")
    changes = changed_protected(repo, "develop", "feature", "develop")
    assert [change.path for change in changes] == [REGISTRY_FILE]


def test_entry_added_on_develop_after_fork_applies(repo: Path) -> None:
    _write(repo, "ci/run.sh", "echo hi\n")
    _commit(repo, "branch work")
    _git(repo, "checkout", "-q", "develop")
    _write(repo, REGISTRY_FILE, 'paths = ["actions/**", "ci/*.sh"]\n')
    _commit(repo, "protect ci scripts")
    _git(repo, "checkout", "-q", "feature")
    changes = changed_protected(repo, "develop", "feature", "develop")
    assert changes == [ProtectedChange("ci/run.sh", _blob(repo, "feature", "ci/run.sh"))]


def test_develop_change_after_fork_is_not_the_branch_change(repo: Path) -> None:
    _git(repo, "checkout", "-q", "develop")
    _write(repo, ".github/workflows/ci.yml", "on: [push]\n")
    _commit(repo, "develop edits a workflow")
    _git(repo, "checkout", "-q", "feature")
    _write(repo, "src/x.py", "x = 4\n")
    _commit(repo, "code")
    assert changed_protected(repo, "develop", "feature", "develop") == []


def test_rename_counts_both_paths_and_delete_needs_deleted_blob(repo: Path) -> None:
    _git(repo, "mv", ".github/workflows/ci.yml", "ci.yml")
    _git(repo, "mv", "src/x.py", ".github/workflows/x.py")
    _git(repo, "rm", "-q", "vergil.toml")
    _commit(repo, "rename and delete")
    changes = changed_protected(repo, "develop", "feature", "develop")
    assert changes == [
        ProtectedChange(".github/workflows/ci.yml", "deleted"),
        ProtectedChange(".github/workflows/x.py", _blob(repo, "feature", ".github/workflows/x.py")),
        ProtectedChange("vergil.toml", "deleted"),
    ]


def test_rename_of_registry_entry_away_counts_old_path(repo: Path) -> None:
    _git(repo, "mv", "actions/setup/action.yml", "action.yml")
    _commit(repo, "move out of the protected tree")
    assert changed_protected(repo, "develop", "feature", "develop") == [
        ProtectedChange("actions/setup/action.yml", "deleted")
    ]


def test_mode_change_is_a_change(repo: Path) -> None:
    _git(repo, "update-index", "--chmod=+x", ".github/workflows/ci.yml")
    _git(repo, "commit", "-q", "-m", "chmod")
    changes = changed_protected(repo, "develop", "feature", "develop")
    assert [change.path for change in changes] == [".github/workflows/ci.yml"]


# -- the registry fails closed -----------------------------------------------------


def test_absent_registry_is_empty(repo: Path) -> None:
    _git(repo, "rm", "-q", REGISTRY_FILE)
    _commit(repo, "no registry")
    assert load_registry(repo, "feature") == ()
    assert protected_set(repo, "feature") == FLEET_DEFAULT


@pytest.mark.parametrize(
    "text",
    [
        "paths = [",
        'paths = "actions/**"',
        "paths = [1]",
        'paths = [""]',
        'paths = ["/abs/**"]',
        'paths = ["./rel"]',
        'paths = ["a/[bc"]',
        'paths = ["a/../b"]',
        'paths = ["a//b"]',
        'paths = ["a/"]',
        'paths = ["a\\\\b"]',
        'paths = ["a**b"]',
        'paths = ["a/[b/c]"]',
        "other = 1",
        'paths = ["a"]\nextra = 1',
        "",
    ],
)
def test_malformed_registry_refuses_not_empty(repo: Path, text: str) -> None:
    _git(repo, "checkout", "-q", "develop")
    _write(repo, REGISTRY_FILE, text)
    _commit(repo, "break the registry")
    _git(repo, "checkout", "-q", "feature")
    with pytest.raises(ProtectedPathsError) as caught:
        load_registry(repo, "develop")
    assert REGISTRY_FILE in str(caught.value)
    assert "develop" in str(caught.value)
    with pytest.raises(ProtectedPathsError):
        changed_protected(repo, "develop", "feature", "develop")


def test_unreadable_ref_refuses(repo: Path) -> None:
    with pytest.raises(ProtectedPathsError) as caught:
        load_registry(repo, "no-such-ref")
    assert "no-such-ref" in str(caught.value)
    with pytest.raises(ProtectedPathsError):
        changed_protected(repo, "develop", "no-such-ref", "develop")
    with pytest.raises(ProtectedPathsError):
        changed_protected(repo, "no-such-ref", "feature", "develop")


@pytest.mark.parametrize("ref", ["", "-rf", "--output=x"])
def test_option_like_ref_refuses(repo: Path, ref: str) -> None:
    with pytest.raises(ProtectedPathsError):
        load_registry(repo, ref)
    with pytest.raises(ProtectedPathsError):
        changed_protected(repo, ref, "feature", "develop")


def test_registry_that_is_a_directory_refuses(repo: Path) -> None:
    _git(repo, "rm", "-q", REGISTRY_FILE)
    _write(repo, f"{REGISTRY_FILE}/x", "x\n")
    _commit(repo, "directory")
    with pytest.raises(ProtectedPathsError, match="not a regular file"):
        load_registry(repo, "feature")


def test_registry_that_is_a_symlink_refuses(repo: Path) -> None:
    _git(repo, "rm", "-q", REGISTRY_FILE)
    (repo / REGISTRY_FILE).symlink_to("vergil.toml")
    _commit(repo, "symlink")
    with pytest.raises(ProtectedPathsError, match="not a regular file"):
        load_registry(repo, "feature")


def test_registry_that_is_not_utf8_refuses(repo: Path) -> None:
    (repo / REGISTRY_FILE).write_bytes(b'paths = ["\xff"]\n')
    _commit(repo, "bad bytes")
    with pytest.raises(ProtectedPathsError, match="UTF-8"):
        load_registry(repo, "feature")


def test_parse_registry_accepts_globs() -> None:
    text = 'paths = ["actions/**", "docs/site/docs/logbook/**", "ci/*.sh", "a?c", "x/[ab].md"]\n'
    assert parse_registry(text, "r") == (
        "actions/**",
        "docs/site/docs/logbook/**",
        "ci/*.sh",
        "a?c",
        "x/[ab].md",
    )


# -- the glob ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "path", "expected"),
    [
        (".github/workflows/**", ".github/workflows/ci.yml", True),
        (".github/workflows/**", ".github/workflows/a/b/c.yml", True),
        (".github/workflows/**", ".github/workflows", False),
        (".github/workflows/**", ".github/workflowsx/ci.yml", False),
        (".github/workflows/**", "x/.github/workflows/ci.yml", False),
        ("vergil.toml", "vergil.toml", True),
        ("vergil.toml", "sub/vergil.toml", False),
        ("vergil.toml", "vergilXtoml", False),
        ("ci/*.sh", "ci/run.sh", True),
        ("ci/*.sh", "ci/deep/run.sh", False),
        ("a?c", "abc", True),
        ("a?c", "a/c", False),
        ("x/[ab].md", "x/a.md", True),
        ("x/[!ab].md", "x/c.md", True),
        ("x/[!ab].md", "x/a.md", False),
        ("**/action.yml", "a/b/action.yml", True),
        ("**/action.yml", "action.yml", True),
        ("**/action.yml", "xaction.yml", False),
        ("docs/**/x.md", "docs/x.md", True),
        ("docs/**/x.md", "docs/a/b/x.md", True),
        ("a.b", "a.b", True),
        ("a.b", "axb", False),
    ],
)
def test_matches(pattern: str, path: str, expected: bool) -> None:
    assert matches(pattern, path) is expected
