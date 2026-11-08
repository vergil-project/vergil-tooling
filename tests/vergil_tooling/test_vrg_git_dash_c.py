"""Tests for vrg-git's global ``-C <path>`` option (#3737).

``-C`` moves the repo every vrg-git check applies to, so each check that
``vrg-git --help`` lists must give the same answer under ``-C <repo>`` as under
``cd <repo>``, and the identity token's org must follow the ``-C`` repo. These
tests run against real git repositories: only the forwarded git command, the
issue read (``gh``) and, for the freeze, the state-file read are stubbed, so
every cwd-derived lookup the checks make really runs.
"""

from __future__ import annotations

import base64
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from vergil_tooling.bin import vrg_git
from vergil_tooling.bin.vrg_git import _split_dash_c, main
from vergil_tooling.lib.pr_workflow.freeze import FreezeCheck

if TYPE_CHECKING:
    from collections.abc import Iterator

_REAL_RUN = subprocess.run
_OPEN_TASK = {"state": "OPEN", "labels": [{"name": "enhancement"}]}


def _git(repo: Path, *args: str) -> None:
    _REAL_RUN(("git", *args), cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(path: Path, origin: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "develop")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("base\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "base commit")
    _git(path, "remote", "add", "origin", origin)
    _git(path, "update-ref", "refs/remotes/origin/develop", "HEAD")
    return path.resolve()


@dataclass(frozen=True)
class Layout:
    """``repo`` (org ``orga``) with a worktree, and an unrelated ``other`` (org ``orgb``)."""

    repo: Path
    worktree: Path
    other: Path


@pytest.fixture
def layout(tmp_path: Path) -> Layout:
    repo = _init_repo(tmp_path / "repo", "https://github.com/orga/repo.git")
    (repo / ".worktrees").mkdir()
    worktree = repo / ".worktrees" / "issue-1-x"
    _git(repo, "worktree", "add", "-b", "feature/1-x", str(worktree))
    # A branch tracking origin/develop: `branch -D` may delete it (#1426).
    _git(repo, "branch", "--track", "feature/2-z", "origin/develop")
    other = _init_repo(tmp_path / "other", "https://github.com/orgb/other.git")
    return Layout(repo=repo, worktree=worktree.resolve(), other=other)


@dataclass
class Outcome:
    rc: int
    forwarded_cwd: Path | None
    forwarded_env: dict[str, str] | None
    issue_read_cwds: list[Path]
    freeze_roots: list[Path]


class _Spy:
    """Stand-in for ``subprocess.run``: stub the forwarded command, run the rest."""

    def __init__(self, forwarded: list[str]) -> None:
        self.forwarded = forwarded
        self.forwarded_cwd: Path | None = None
        self.forwarded_env: dict[str, str] | None = None

    def __call__(self, cmd: Any, *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if list(cmd) == self.forwarded:
            self.forwarded_cwd = Path(kwargs.get("cwd") or Path.cwd()).resolve()
            self.forwarded_env = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return _REAL_RUN(cmd, *args, **kwargs)


@pytest.fixture
def run_vrg_git(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    def _run(start: Path, argv: list[str], *, frozen: bool = False) -> Outcome:
        _, rest = _split_dash_c(argv)
        spy = _Spy(["git", *rest])
        issue_read_cwds: list[Path] = []
        freeze_roots: list[Path] = []

        def _read_json(*_args: str) -> dict[str, object]:
            issue_read_cwds.append(Path.cwd().resolve())
            return _OPEN_TASK

        def _check_worktree(root: Path, *, action: str) -> FreezeCheck:
            freeze_roots.append(root.resolve())
            if frozen:
                return FreezeCheck(frozen=True, message=f"FROZEN ({action})")
            return FreezeCheck(frozen=False)

        monkeypatch.chdir(start)
        with (
            patch.object(vrg_git.subprocess, "run", spy),
            patch.object(vrg_git.github, "read_json", _read_json),
            patch.object(vrg_git.freeze, "check_worktree", _check_worktree),
            patch.dict(
                vrg_git.github._token_cache,
                {
                    "orga": ("tok-orga", time.time() + 3000),
                    "orgb": ("tok-orgb", time.time() + 3000),
                },
            ),
        ):
            rc = main(argv)
        # vrg-git never leaves the process in the -C directory.
        assert Path.cwd().resolve() == start.resolve()
        return Outcome(
            rc=rc,
            forwarded_cwd=spy.forwarded_cwd,
            forwarded_env=spy.forwarded_env,
            issue_read_cwds=issue_read_cwds,
            freeze_roots=freeze_roots,
        )

    yield _run


def _token_of(outcome: Outcome) -> str | None:
    env = outcome.forwarded_env
    if env is None:
        return None
    header = env["GIT_CONFIG_VALUE_0"]
    encoded = header.rsplit(" ", 1)[-1]
    return base64.b64decode(encoded).decode().split(":", 1)[1]


# Each case: (target attribute, start attribute, argv, expected rc in target).
# The start directory is chosen so that, where the check depends on the cwd,
# running the same argv *there* would answer differently: the answer under -C
# then proves the check followed the -C path, not the start directory.
_CASES = [
    # Worktree convention: a branch switch in the main worktree is refused.
    pytest.param("repo", "other", ["checkout", "feature/1-x"], 1, id="main-worktree-switch"),
    # ...and allowed in a secondary worktree, even when started in the main one.
    pytest.param("worktree", "repo", ["switch", "feature/1-x"], 0, id="secondary-worktree-switch"),
    # worktree add anchoring: a relative path resolves against the -C repo, so
    # it is allowed from the root even when started inside a worktree...
    pytest.param(
        "repo",
        "worktree",
        ["worktree", "add", "-b", "feature/7-y", ".worktrees/issue-7-y", "origin/develop"],
        0,
        id="worktree-add-from-root",
    ),
    # ...and refused (nesting) when -C names a worktree.
    pytest.param(
        "worktree",
        "repo",
        ["worktree", "add", "-b", "feature/7-y", ".worktrees/issue-7-y", "origin/develop"],
        1,
        id="worktree-add-nested",
    ),
    # Branch/directory lockstep.
    pytest.param(
        "repo",
        "other",
        ["worktree", "add", "-b", "feature/7-y", ".worktrees/issue-8-y", "origin/develop"],
        1,
        id="worktree-add-lockstep",
    ),
    # Issue-family branch form at creation.
    pytest.param("worktree", "other", ["checkout", "-b", "feature/bad"], 1, id="branch-form"),
    # Protected-branch force: --force-with-lease is refused on develop...
    pytest.param(
        "repo",
        "worktree",
        ["push", "--force-with-lease", "origin", "develop"],
        1,
        id="force-with-lease-protected",
    ),
    # ...and allowed on a feature branch, even when started on develop.
    pytest.param(
        "worktree",
        "repo",
        ["push", "--force-with-lease", "origin", "feature/1-x"],
        0,
        id="force-with-lease-feature",
    ),
    # branch -D: allowed for a branch tracking origin/develop in the -C repo,
    # which the start repo does not have.
    pytest.param("repo", "other", ["branch", "-D", "feature/2-z"], 0, id="branch-delete"),
    # Flag deny-list and denied / unrecognized subcommands.
    pytest.param("repo", "other", ["push", "--force", "origin", "x"], 1, id="push-force"),
    pytest.param("repo", "other", ["rebase", "-i", "develop"], 1, id="rebase-interactive"),
    pytest.param("repo", "other", ["commit", "-m", "x"], 1, id="denied-subcommand"),
    pytest.param("repo", "other", ["reflog", "expire"], 1, id="reflog-expire"),
    pytest.param("repo", "other", ["symbolic-ref", "HEAD", "x"], 1, id="symbolic-ref-write"),
    pytest.param("repo", "other", ["status"], 0, id="read-only"),
]


@pytest.mark.parametrize(("target", "start", "argv", "expected"), _CASES)
def test_dash_c_gives_the_same_answer_as_cd(
    layout: Layout, run_vrg_git: Any, target: str, start: str, argv: list[str], expected: int
) -> None:
    target_dir: Path = getattr(layout, target)
    start_dir: Path = getattr(layout, start)
    via_cd = run_vrg_git(target_dir, argv)
    via_dash_c = run_vrg_git(start_dir, ["-C", str(target_dir), *argv])
    assert via_cd.rc == expected
    assert via_dash_c.rc == via_cd.rc
    if expected == 0:
        # git itself runs in the target, with the -C option not forwarded.
        assert via_cd.forwarded_cwd == target_dir
        assert via_dash_c.forwarded_cwd == target_dir
    else:
        assert via_dash_c.forwarded_cwd is None
    assert via_dash_c.issue_read_cwds == via_cd.issue_read_cwds


_CWD_DEPENDENT = {
    "main-worktree-switch",
    "secondary-worktree-switch",
    "worktree-add-from-root",
    "worktree-add-nested",
    "force-with-lease-protected",
    "force-with-lease-feature",
    "branch-delete",
}


@pytest.mark.parametrize(
    ("target", "start", "argv", "expected"),
    [p for p in _CASES if p.id in _CWD_DEPENDENT],
)
def test_cwd_dependent_checks_answer_differently_from_the_start_dir(
    layout: Layout, run_vrg_git: Any, target: str, start: str, argv: list[str], expected: int
) -> None:
    # Guards the cases above against passing vacuously: without -C, the start
    # directory gives the other answer.
    from_start = run_vrg_git(getattr(layout, start), argv)
    assert from_start.rc != expected


def test_issue_read_runs_in_the_dash_c_repo(layout: Layout, run_vrg_git: Any) -> None:
    out = run_vrg_git(
        layout.other,
        ["-C", str(layout.repo), "worktree", "add", "-b", "feature/7-y", ".worktrees/issue-7-y"],
    )
    assert out.rc == 0
    assert out.issue_read_cwds == [layout.repo]


def test_freeze_check_reads_the_dash_c_worktree(
    layout: Layout, run_vrg_git: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_vrg_git(
        layout.other,
        ["-C", str(layout.worktree), "push", "origin", "feature/1-x"],
        frozen=True,
    )
    assert out.rc == 1
    assert out.forwarded_cwd is None
    assert out.freeze_roots == [layout.worktree]
    assert "FROZEN" in capsys.readouterr().err


@pytest.mark.parametrize("subcmd", ["push", "fetch", "pull", "ls-remote"])
def test_token_org_follows_the_dash_c_repo(
    layout: Layout, run_vrg_git: Any, subcmd: str
) -> None:
    via_dash_c = run_vrg_git(layout.other, ["-C", str(layout.worktree), subcmd, "origin"])
    via_cd = run_vrg_git(layout.worktree, [subcmd, "origin"])
    from_start = run_vrg_git(layout.other, [subcmd, "origin"])
    assert via_dash_c.rc == 0
    assert _token_of(via_dash_c) == "tok-orga"
    assert _token_of(via_cd) == "tok-orga"
    assert _token_of(from_start) == "tok-orgb"
    assert via_dash_c.forwarded_cwd == layout.worktree


def test_repeated_dash_c_chains_like_git(layout: Layout, run_vrg_git: Any) -> None:
    # Each -C is relative to the one before; an empty one is a no-op.
    out = run_vrg_git(
        layout.other,
        ["-C", str(layout.repo), "-C", "", "-C", ".worktrees/issue-1-x", "status"],
    )
    assert out.rc == 0
    assert out.forwarded_cwd == layout.worktree


def test_dash_c_missing_path_refuses_without_running_git(
    layout: Layout, run_vrg_git: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_vrg_git(layout.other, ["-C", str(layout.other / "nope"), "status"])
    assert out.rc == 128
    assert out.forwarded_cwd is None
    assert "cannot change to" in capsys.readouterr().err


def test_dash_c_without_value_is_a_usage_error(
    layout: Layout, run_vrg_git: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_vrg_git(layout.other, ["-C"])
    assert out.rc == 2
    assert "requires a value" in capsys.readouterr().err


def test_dash_c_with_no_subcommand_prints_usage(
    layout: Layout, run_vrg_git: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_vrg_git(layout.other, ["-C", str(layout.repo)])
    assert out.rc == 2
    assert "usage" in capsys.readouterr().err.lower()


def test_dash_c_then_help_prints_wrapper_help(
    layout: Layout, run_vrg_git: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_vrg_git(layout.other, ["-C", str(layout.repo), "--help"])
    assert out.rc == 0
    assert "-C <path>" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["-c", "core.hooksPath=/x", "status"],
        ["--git-dir=/x", "status"],
        ["--work-tree=/x", "status"],
        ["-C/x", "status"],
        ["-C", ".", "-c", "core.hooksPath=/x", "status"],
        ["-C", ".", "--git-dir=/x", "status"],
    ],
)
def test_other_global_options_stay_refused(
    layout: Layout, run_vrg_git: Any, argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    out = run_vrg_git(layout.repo, argv)
    assert out.rc == 1
    assert out.forwarded_cwd is None
    assert "is not recognized" in capsys.readouterr().err


def test_split_dash_c() -> None:
    assert _split_dash_c(["status"]) == ([], ["status"])
    assert _split_dash_c(["-C", "a", "-C", "b", "log", "-C"]) == (["a", "b"], ["log", "-C"])
    assert _split_dash_c(["-C", "a", "-C"]) == (None, [])
    assert _split_dash_c([]) == ([], [])
