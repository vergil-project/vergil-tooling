"""Tests for ``vrg-state-dir``, the task state directory helper."""

from __future__ import annotations

import stat
import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.bin import vrg_state_dir

if TYPE_CHECKING:
    from pathlib import Path

Cap = pytest.CaptureFixture[str]

ISSUE = "acme/widget#42"
ISSUE_REPO_URL = "https://github.com/acme/widget.git"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A main clone of the issue's repository, with one commit, as the cwd."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    _git(path, "remote", "add", "origin", ISSUE_REPO_URL)
    (path / ".gitignore").write_text("build/\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(path)
    return path


def _run(argv: list[str], capsys: Cap) -> tuple[int, str, str]:
    code = vrg_state_dir.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _argv(kind: str = "operational", issue: str = ISSUE) -> list[str]:
    return ["--kind", kind, "--issue", issue, "--ensure"]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


@pytest.mark.parametrize("kind", ["operational", "issue-implement", "upstream-report"])
def test_ensure_creates_each_level_700_and_prints_the_path(
    repo: Path, capsys: Cap, kind: str
) -> None:
    code, out, err = _run(_argv(kind), capsys)
    assert (code, err) == (0, "")
    state = repo / "build" / ".vergil-state"
    directory = state / kind / "42"
    assert out == f"{directory}\n"
    assert [_mode(level) for level in (state, state / kind, directory)] == [0o700] * 3
    assert list(directory.iterdir()) == []


def test_ensure_accepts_an_existing_private_directory(repo: Path, capsys: Cap) -> None:
    _run(_argv(), capsys)
    directory = repo / "build" / ".vergil-state" / "operational" / "42"
    (directory / "security-1.md").write_text("kept")
    code, out, _err = _run(_argv(), capsys)
    assert code == 0
    assert out == f"{directory}\n"
    assert (directory / "security-1.md").read_text() == "kept"


def test_ensure_from_a_linked_worktree_uses_the_main_clone(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Cap
) -> None:
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "other", str(linked), "develop")
    monkeypatch.chdir(linked)
    code, out, _err = _run(_argv(), capsys)
    assert code == 0
    assert out == f"{repo / 'build' / '.vergil-state' / 'operational' / '42'}\n"
    assert not (linked / "build").exists()


def test_ensure_refuses_a_symlinked_level(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    state = repo / "build" / ".vergil-state"
    state.mkdir(parents=True, mode=0o700)
    (state / "operational").symlink_to(elsewhere)
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "operational is a symlink, not a directory" in err
    assert list(elsewhere.iterdir()) == []


def test_ensure_refuses_a_non_directory_level(repo: Path, capsys: Cap) -> None:
    _run(_argv(issue="acme/widget#41"), capsys)
    (repo / "build" / ".vergil-state" / "operational" / "42").write_text("a file")
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "42 is not a directory" in err


def test_ensure_refuses_a_wrong_mode(repo: Path, capsys: Cap) -> None:
    _run(_argv(), capsys)
    directory = repo / "build" / ".vergil-state" / "operational" / "42"
    directory.chmod(0o755)
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "42 has mode 755, not 700" in err


def test_ensure_refuses_a_symlinked_build(repo: Path, tmp_path: Path, capsys: Cap) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (repo / "build").symlink_to(elsewhere)
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "build is a symlink, not a directory" in err
    assert list(elsewhere.iterdir()) == []


def test_ensure_refuses_a_clone_of_another_repo(repo: Path, capsys: Cap) -> None:
    _git(repo, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "pushes to acme/other, not the issue's repo acme/widget" in err
    assert "run vrg-state-dir in a clone of acme/widget" in err
    assert not (repo / "build").exists()


def test_ensure_refuses_a_clone_without_origin(repo: Path, capsys: Cap) -> None:
    _git(repo, "remote", "remove", "origin")
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "cannot read the push URL of the 'origin' remote" in err
    assert not (repo / "build").exists()


def test_ensure_reports_a_git_failure(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: Cap
) -> None:
    def fail() -> Path:
        raise subprocess.CalledProcessError(128, ["git", "rev-parse", "--git-common-dir"])

    monkeypatch.setattr(vrg_state_dir.git, "main_worktree_root", fail)
    code, out, err = _run(_argv(), capsys)
    assert code == 1
    assert out == ""
    assert "ERROR: git failed (exit 128): git rev-parse --git-common-dir" in err
    assert not (repo / "build").exists()


@pytest.mark.parametrize("issue", ["widget#42", "acme/widget", "#42", "acme/widget#0"])
def test_ensure_refuses_a_bad_issue_ref(repo: Path, capsys: Cap, issue: str) -> None:
    code, out, err = _run(_argv(issue=issue), capsys)
    assert code == 1
    assert out == ""
    assert "--issue must" in err
    assert not (repo / "build").exists()


DESIGN_ARGS = ["--kind", "design-review", "--issue", ISSUE, "--round", "S1", "--ensure"]
HOSHIN_REPO = "acme/widget"
HOSHIN_ARGS = [
    "--kind",
    "hoshin",
    "--repo",
    HOSHIN_REPO,
    "--date",
    "2026-11-28",
    "--mode",
    "start",
    "--ensure",
]


def test_design_review_creates_levels_700_and_prints_the_round_path(
    repo: Path, capsys: Cap
) -> None:
    argv = ["--kind", "design-review", "--issue", ISSUE, "--round", "P12", "--ensure"]
    code, out, err = _run(argv, capsys)
    assert (code, err) == (0, "")
    state = repo / "build" / ".vergil-state"
    issue_dir = state / "design-review" / "42"
    directory = issue_dir / "P12"
    assert out == f"{directory}\n"
    levels = [state, state / "design-review", issue_dir, directory]
    assert [_mode(level) for level in levels] == [0o700] * 4
    assert list(directory.iterdir()) == []


def test_design_review_accepts_an_existing_round_directory(repo: Path, capsys: Cap) -> None:
    _run(DESIGN_ARGS, capsys)
    directory = repo / "build" / ".vergil-state" / "design-review" / "42" / "S1"
    (directory / "review.md").write_text("kept")
    code, out, _err = _run(DESIGN_ARGS, capsys)
    assert code == 0
    assert out == f"{directory}\n"
    assert (directory / "review.md").read_text() == "kept"


def test_design_review_refuses_a_clone_of_another_repo(repo: Path, capsys: Cap) -> None:
    _git(repo, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    code, out, err = _run(DESIGN_ARGS, capsys)
    assert code == 1
    assert out == ""
    assert "pushes to acme/other, not the issue's repo acme/widget" in err
    assert not (repo / "build").exists()


def test_design_review_refuses_a_symlinked_round_level(
    repo: Path, tmp_path: Path, capsys: Cap
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    state = repo / "build" / ".vergil-state"
    state.mkdir(parents=True)
    state.chmod(0o700)
    design = state / "design-review"
    design.mkdir(mode=0o700)
    issue_dir = design / "42"
    issue_dir.mkdir(mode=0o700)
    (issue_dir / "S1").symlink_to(elsewhere)
    code, out, err = _run(DESIGN_ARGS, capsys)
    assert code == 1
    assert out == ""
    assert "S1 is a symlink, not a directory" in err
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize(("date", "mode"), [("2028-02-29", "end"), ("2026-11-28", "start")])
def test_hoshin_creates_levels_700_and_prints_the_path(
    repo: Path, capsys: Cap, date: str, mode: str
) -> None:
    argv = ["--kind", "hoshin", "--repo", HOSHIN_REPO, "--date", date, "--mode", mode, "--ensure"]
    code, out, err = _run(argv, capsys)
    assert (code, err) == (0, "")
    state = repo / "build" / ".vergil-state"
    directory = state / "hoshin" / f"{date}-{mode}"
    assert out == f"{directory}\n"
    assert [_mode(level) for level in (state, state / "hoshin", directory)] == [0o700] * 3
    assert list(directory.iterdir()) == []


def test_hoshin_accepts_an_existing_directory(repo: Path, capsys: Cap) -> None:
    _run(HOSHIN_ARGS, capsys)
    directory = repo / "build" / ".vergil-state" / "hoshin" / "2026-11-28-start"
    (directory / "screen-terms.md").write_text("kept")
    code, out, _err = _run(HOSHIN_ARGS, capsys)
    assert code == 0
    assert out == f"{directory}\n"
    assert (directory / "screen-terms.md").read_text() == "kept"


def test_hoshin_refuses_a_clone_of_another_repo(repo: Path, capsys: Cap) -> None:
    _git(repo, "remote", "set-url", "origin", "https://github.com/acme/other.git")
    code, out, err = _run(HOSHIN_ARGS, capsys)
    assert code == 1
    assert out == ""
    assert "pushes to acme/other, not the repo named by --repo acme/widget" in err
    assert "run vrg-state-dir in a clone of acme/widget" in err
    assert not (repo / "build").exists()


def test_hoshin_checks_the_repo_named_by_repo_not_the_issue_default(
    repo: Path, capsys: Cap
) -> None:
    argv = [*HOSHIN_ARGS[:3], "acme/other", *HOSHIN_ARGS[4:]]
    code, out, err = _run(argv, capsys)
    assert code == 1
    assert out == ""
    assert "pushes to acme/widget, not the repo named by --repo acme/other" in err
    assert not (repo / "build").exists()


def test_hoshin_refuses_a_symlinked_date_mode_level(
    repo: Path, tmp_path: Path, capsys: Cap
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    state = repo / "build" / ".vergil-state"
    state.mkdir(parents=True)
    state.chmod(0o700)
    hoshin = state / "hoshin"
    hoshin.mkdir(mode=0o700)
    (hoshin / "2026-11-28-start").symlink_to(elsewhere)
    code, out, err = _run(HOSHIN_ARGS, capsys)
    assert code == 1
    assert out == ""
    assert "2026-11-28-start is a symlink, not a directory" in err
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        # The original usage errors.
        (["--kind", "operational", "--issue", ISSUE], "--ensure"),
        (["--issue", ISSUE, "--ensure"], "--kind"),
        (["--kind", "operational", "--ensure"], "--kind operational requires --issue"),
        # design-review: --round is required, --issue too, and no hoshin option.
        (["--kind", "design-review", "--issue", ISSUE, "--ensure"], "requires --round"),
        (["--kind", "design-review", "--round", "S1", "--ensure"], "requires --issue"),
        (["--kind", "design-review", "--round", "X1", "--issue", ISSUE, "--ensure"], "--round"),
        (["--kind", "design-review", "--round", "S", "--issue", ISSUE, "--ensure"], "--round"),
        (["--kind", "design-review", "--round", "S1x", "--issue", ISSUE, "--ensure"], "--round"),
        (["--kind", "design-review", "--round", "", "--issue", ISSUE, "--ensure"], "--round"),
        ([*DESIGN_ARGS, "--date", "2026-11-28"], "--date does not apply"),
        ([*DESIGN_ARGS, "--mode", "start"], "--mode does not apply"),
        ([*DESIGN_ARGS, "--repo", HOSHIN_REPO], "--repo does not apply"),
        # hoshin: --repo, --date and --mode are required, and --issue and --round do not apply.
        (
            ["--kind", "hoshin", "--date", "2026-11-28", "--mode", "start", "--ensure"],
            "requires --repo",
        ),
        (
            ["--kind", "hoshin", "--repo", HOSHIN_REPO, "--mode", "start", "--ensure"],
            "requires --date",
        ),
        (
            ["--kind", "hoshin", "--repo", HOSHIN_REPO, "--date", "2026-11-28", "--ensure"],
            "requires --mode",
        ),
        ([*HOSHIN_ARGS, "--issue", ISSUE], "--issue does not apply"),
        ([*HOSHIN_ARGS, "--round", "S1"], "--round does not apply"),
        # Options of the other kinds, on the issue-numbered kinds.
        (
            ["--kind", "operational", "--issue", ISSUE, "--round", "S1", "--ensure"],
            "--round does not apply",
        ),
        (
            ["--kind", "issue-implement", "--issue", ISSUE, "--date", "2026-11-28", "--ensure"],
            "--date does not apply",
        ),
        (
            ["--kind", "upstream-report", "--issue", ISSUE, "--mode", "end", "--ensure"],
            "--mode does not apply",
        ),
        (
            ["--kind", "operational", "--issue", ISSUE, "--repo", HOSHIN_REPO, "--ensure"],
            "--repo does not apply",
        ),
        # Malformed hoshin values.
        (
            [
                "--kind",
                "hoshin",
                "--repo",
                HOSHIN_REPO,
                "--date",
                "2026-13-40",
                "--mode",
                "start",
                "--ensure",
            ],
            "--date",
        ),
        (
            [
                "--kind",
                "hoshin",
                "--repo",
                HOSHIN_REPO,
                "--date",
                "20261128",
                "--mode",
                "start",
                "--ensure",
            ],
            "--date",
        ),
        (
            [
                "--kind",
                "hoshin",
                "--repo",
                HOSHIN_REPO,
                "--date",
                "2026-02-29",
                "--mode",
                "start",
                "--ensure",
            ],
            "--date",
        ),
        (
            [
                "--kind",
                "hoshin",
                "--repo",
                HOSHIN_REPO,
                "--date",
                "2026-11-28",
                "--mode",
                "middle",
                "--ensure",
            ],
            "--mode",
        ),
        (
            [
                "--kind",
                "hoshin",
                "--repo",
                "acme",
                "--date",
                "2026-11-28",
                "--mode",
                "start",
                "--ensure",
            ],
            "--repo",
        ),
        (
            [
                "--kind",
                "hoshin",
                "--repo",
                "acme/",
                "--date",
                "2026-11-28",
                "--mode",
                "start",
                "--ensure",
            ],
            "--repo",
        ),
        # A kind name outside the five.
        (["--kind", "merge-review", "--issue", ISSUE, "--ensure"], "--kind"),
    ],
)
def test_usage_errors_exit_2(repo: Path, capsys: Cap, argv: list[str], fragment: str) -> None:
    with pytest.raises(SystemExit) as exc:
        vrg_state_dir.main(argv)
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert fragment in captured.err
    assert not (repo / "build").exists()
