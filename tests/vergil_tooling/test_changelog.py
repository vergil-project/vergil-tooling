"""Tests for vergil_tooling.lib.changelog."""

from __future__ import annotations

import subprocess as _sp
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from vergil_tooling.lib.changelog import (
    BOUNDARY_TAG_PATTERN,
    RELEASE_NOTES_DIR,
    generate_changelog,
    generate_release_notes,
    previous_release_tag,
    release_boundary,
)

if TYPE_CHECKING:
    from pathlib import Path
    from unittest.mock import MagicMock

_MOD = "vergil_tooling.lib.changelog"
_OK = _sp.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# --- git-cliff invocation (git-cliff mocked) ------------------------------------


def _cliff_args(mock_run: MagicMock) -> tuple[str, ...]:
    return tuple(mock_run.call_args[0][0])


def test_generate_changelog_prepends_from_the_boundary(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".release_boundary", return_value="abc123"),
        patch(_MOD + "._published_changelog", return_value="# Changelog\n\n## [1.0.0]\n"),
        patch(_MOD + ".subprocess.run", return_value=_OK) as mock_run,
    ):
        generate_changelog(tmp_path, "2.0.34")
    args = _cliff_args(mock_run)
    assert args[0] == "git-cliff"
    assert args[args.index("--tag") + 1] == "develop-v2.0.34"
    assert args[args.index("--ignore-tags") + 1] == BOUNDARY_TAG_PATTERN
    assert args[args.index("--prepend") + 1] == str(tmp_path / "CHANGELOG.md")
    assert "-o" not in args
    assert args[-1] == "abc123..HEAD"
    # The published changelog is restored before prepending, so a re-run
    # never prepends twice.
    assert (tmp_path / "CHANGELOG.md").read_text() == "# Changelog\n\n## [1.0.0]\n"


def test_generate_changelog_without_published_file_writes_it(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".release_boundary", return_value="abc123"),
        patch(_MOD + "._published_changelog", return_value=None),
        patch(_MOD + ".subprocess.run", return_value=_OK) as mock_run,
    ):
        (tmp_path / "CHANGELOG.md").write_text("x\n")
        generate_changelog(tmp_path, "1.0.0")
    args = _cliff_args(mock_run)
    assert args[args.index("-o") + 1] == str(tmp_path / "CHANGELOG.md")
    assert "--prepend" not in args
    assert args[-1] == "abc123..HEAD"


def test_generate_changelog_first_release_covers_all_history(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".release_boundary", return_value=None),
        patch(_MOD + "._published_changelog", return_value="old\n"),
        patch(_MOD + ".subprocess.run", return_value=_OK) as mock_run,
    ):
        (tmp_path / "CHANGELOG.md").write_text("x\n")
        generate_changelog(tmp_path, "1.0.0")
    args = _cliff_args(mock_run)
    assert "-o" in args
    assert "--prepend" not in args
    assert not any(".." in arg for arg in args)


def test_generate_changelog_normalizes_trailing_newline(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("content\n\n\n")
    with (
        patch(_MOD + ".release_boundary", return_value=None),
        patch(_MOD + "._published_changelog", return_value=None),
        patch(_MOD + ".subprocess.run", return_value=_OK),
    ):
        generate_changelog(tmp_path, "1.0.0")
    assert changelog.read_text().endswith("\n")
    assert not changelog.read_text().endswith("\n\n")


def test_generate_release_notes_uses_the_boundary_range(tmp_path: Path) -> None:
    output = tmp_path / RELEASE_NOTES_DIR / "v2.0.34.md"
    with (
        patch(_MOD + ".release_boundary", return_value="abc123"),
        patch(_MOD + ".subprocess.run", return_value=_OK) as mock_run,
    ):
        output.parent.mkdir()
        output.write_text("notes\n")
        result = generate_release_notes(tmp_path, "2.0.34")
    assert result == output
    args = _cliff_args(mock_run)
    assert args[args.index("--tag") + 1] == "develop-v2.0.34"
    assert args[args.index("--ignore-tags") + 1] == BOUNDARY_TAG_PATTERN
    assert args[args.index("-o") + 1] == str(output)
    assert "--unreleased" not in args
    assert args[-1] == "abc123..HEAD"


def test_generate_release_notes_creates_dir(tmp_path: Path) -> None:
    def _cliff(*_a: object, **_k: object) -> _sp.CompletedProcess[str]:
        (tmp_path / RELEASE_NOTES_DIR / "v1.0.0.md").write_text("notes\n")
        return _OK

    with (
        patch(_MOD + ".release_boundary", return_value=None),
        patch(_MOD + ".subprocess.run", side_effect=_cliff),
    ):
        generate_release_notes(tmp_path, "1.0.0")
    assert (tmp_path / RELEASE_NOTES_DIR).is_dir()


def _cliff_failure(stderr: str) -> _sp.CalledProcessError:
    err = _sp.CalledProcessError(1, "git-cliff")
    err.stderr = stderr
    err.stdout = ""
    return err


def test_generate_changelog_raises_and_prints_stderr_on_cliff_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(_MOD + ".release_boundary", return_value=None),
        patch(_MOD + "._published_changelog", return_value=None),
        patch(_MOD + ".subprocess.run", side_effect=_cliff_failure("error: unknown option\n")),
        pytest.raises(_sp.CalledProcessError),
    ):
        generate_changelog(tmp_path, "1.0.0")
    assert "unknown option" in capsys.readouterr().err


def test_generate_changelog_raises_silently_on_failure_without_stderr(tmp_path: Path) -> None:
    with (
        patch(_MOD + ".release_boundary", return_value=None),
        patch(_MOD + "._published_changelog", return_value=None),
        patch(_MOD + ".subprocess.run", side_effect=_cliff_failure("")),
        pytest.raises(_sp.CalledProcessError),
    ):
        generate_changelog(tmp_path, "1.0.0")


def test_generate_release_notes_raises_and_prints_stderr_on_cliff_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with (
        patch(_MOD + ".release_boundary", return_value=None),
        patch(_MOD + ".subprocess.run", side_effect=_cliff_failure("error: unknown flag\n")),
        pytest.raises(_sp.CalledProcessError),
    ):
        generate_release_notes(tmp_path, "1.0.0")
    assert "unknown flag" in capsys.readouterr().err


def test_generate_changelog_prints_stdout_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "CHANGELOG.md").write_text("old\n")
    out = _sp.CompletedProcess(
        args=[], returncode=0, stdout="cliff output\n", stderr="cliff warning\n"
    )
    with (
        patch(_MOD + ".release_boundary", return_value=None),
        patch(_MOD + "._published_changelog", return_value=None),
        patch(_MOD + ".subprocess.run", return_value=out),
    ):
        generate_changelog(tmp_path, "1.0.0")
    captured = capsys.readouterr()
    assert "cliff output" in captured.out
    assert "cliff warning" in captured.err


def test_generate_release_notes_prints_stdout_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / RELEASE_NOTES_DIR / "v1.0.0.md"
    output.parent.mkdir()
    output.write_text("notes\n")
    out = _sp.CompletedProcess(
        args=[], returncode=0, stdout="notes output\n", stderr="notes warning\n"
    )
    with (
        patch(_MOD + ".release_boundary", return_value=None),
        patch(_MOD + ".subprocess.run", return_value=out),
    ):
        generate_release_notes(tmp_path, "1.0.0")
    captured = capsys.readouterr()
    assert "notes output" in captured.out
    assert "notes warning" in captured.err


# --- fixture repository histories (real git and git-cliff) ---------------------


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "develop")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "config", "tag.gpgsign", "false")
    return root


def _git(root: Path, *args: str) -> str:
    result = _sp.run(  # noqa: S603
        ("git", *args),  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(root: Path, message: str) -> None:
    _git(root, "commit", "-q", "--allow-empty", "-m", message)


def _prepare(root: Path, version: str) -> tuple[str, str]:
    """Run the prepare stage's generation and commit, as vrg-release does."""
    generate_changelog(root, version)
    notes = generate_release_notes(root, version)
    _git(root, "add", "CHANGELOG.md", str(notes))
    _commit(root, f"chore(release): prepare {version}")
    return (root / "CHANGELOG.md").read_text(), notes.read_text()


def _publish(root: Path, version: str, release_branch: str) -> None:
    """Merge the release branch to main and tag it, as the release PR and CD do."""
    _git(root, "switch", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "-m", f"Merge {release_branch}", release_branch)
    _git(root, "tag", "-a", f"v{version}", "-m", f"v{version}")


def _cut_release(root: Path, version: str) -> None:
    _git(root, "switch", "-q", "-c", f"release/{version}", "develop")


def _back_merge(root: Path, version: str) -> None:
    """Merge main back into develop with the version bump (the post-release PR)."""
    branch = f"release/post-{version}"
    _git(root, "switch", "-q", "-c", branch, "develop")
    _git(root, "merge", "-q", "--no-ff", "-m", "Merge main", "main")
    _commit(root, f"chore(release): bump version after {version}")
    _git(root, "switch", "-q", "develop")
    _git(root, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", branch)


def test_pr_merged_while_release_in_flight_ships_in_the_next_release(repo: Path) -> None:
    _commit(repo, "feat: initial feature")
    _git(repo, "branch", "main")
    _commit(repo, "feat: first release feature")

    # Release 1.0.0 is cut, then a PR lands on develop while it is in flight.
    _cut_release(repo, "1.0.0")
    changelog_1_0, notes_1_0 = _prepare(repo, "1.0.0")
    _git(repo, "switch", "-q", "develop")
    _commit(repo, "feat: merged while in flight")
    _publish(repo, "1.0.0", "release/1.0.0")
    # CD places the boundary tag on the develop tip, past the in-flight PR.
    _git(repo, "tag", "develop-v1.0.0", "develop")
    _back_merge(repo, "1.0.0")
    _commit(repo, "fix: next release fix")

    _cut_release(repo, "1.1.0")
    changelog_1_1, notes_1_1 = _prepare(repo, "1.1.0")

    assert "first release feature" in notes_1_0
    assert "merged while in flight" not in notes_1_0
    assert "merged while in flight" not in changelog_1_0

    assert "merged while in flight" in notes_1_1
    assert "next release fix" in notes_1_1
    assert "first release feature" not in notes_1_1
    assert "initial feature" not in notes_1_1

    # The 1.1.0 section is prepended; the published 1.0.0 entry is unchanged.
    new_section, published = changelog_1_1.split("## [1.0.0]")
    assert "merged while in flight" in new_section
    assert "next release fix" in new_section
    assert "## [1.0.0]" + published == changelog_1_0[changelog_1_0.index("## [1.0.0]") :]
    assert changelog_1_1.count("# Changelog") == 1


def test_regenerating_the_changelog_is_idempotent(repo: Path) -> None:
    _commit(repo, "feat: initial feature")
    _git(repo, "branch", "main")
    _cut_release(repo, "1.0.0")
    _prepare(repo, "1.0.0")
    _publish(repo, "1.0.0", "release/1.0.0")
    _git(repo, "tag", "develop-v1.0.0", "develop")
    _back_merge(repo, "1.0.0")
    _commit(repo, "feat: second")
    _cut_release(repo, "1.1.0")

    generate_changelog(repo, "1.1.0")
    first = (repo / "CHANGELOG.md").read_text()
    generate_changelog(repo, "1.1.0")
    assert (repo / "CHANGELOG.md").read_text() == first
    assert first.count("## [1.1.0]") == 1


def test_previous_release_tag_is_the_highest_release_below_version(repo: Path) -> None:
    _commit(repo, "feat: x")
    for tag in ("v1.9.0", "v1.10.0", "v1.10", "v2.0.0", "develop-v1.11.0", "v1.10.0-rc1"):
        _git(repo, "tag", tag)
    assert previous_release_tag(repo, "1.11.0") == "v1.10.0"
    assert previous_release_tag(repo, "1.10.0") == "v1.9.0"
    assert previous_release_tag(repo, "1.9.0") is None


def test_previous_release_tag_rejects_a_non_release_version(repo: Path) -> None:
    with pytest.raises(ValueError, match="not a release version"):
        previous_release_tag(repo, "1.2")


def test_release_boundary_is_the_merge_base_with_the_previous_release(repo: Path) -> None:
    _commit(repo, "feat: base")
    _git(repo, "branch", "main")
    fork = _git(repo, "rev-parse", "HEAD")
    _git(repo, "switch", "-q", "main")
    _commit(repo, "chore(release): prepare 1.0.0")
    _git(repo, "tag", "-a", "v1.0.0", "-m", "v1.0.0")
    _git(repo, "switch", "-q", "develop")
    _commit(repo, "feat: later")
    # v1.0.0 is not yet merged back: the boundary is where it forked.
    assert release_boundary(repo, "1.1.0") == fork
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge main", "main")
    assert release_boundary(repo, "1.1.0") == _git(repo, "rev-parse", "v1.0.0^{commit}")


def test_release_boundary_is_none_for_a_first_release(repo: Path) -> None:
    _commit(repo, "feat: base")
    assert release_boundary(repo, "1.0.0") is None
