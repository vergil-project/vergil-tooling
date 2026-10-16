"""Tests for the repo-hook updater (spec §6.2, epic vergil-project/.github#381)."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from vergil_tooling.lib.update_deps import report
from vergil_tooling.lib.update_deps.context import UpdateDepsContext, UpdateDepsError
from vergil_tooling.lib.update_deps.report import HeldBack
from vergil_tooling.lib.update_deps.updaters.repo_hook import RepoHookUpdater

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_MOD = "vergil_tooling.lib.update_deps.updaters.repo_hook"


def _ctx(root: Path, *, worktree: bool = True) -> UpdateDepsContext:
    return UpdateDepsContext(repo="o/r", repo_root=root, worktree_path=root if worktree else None)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    hook: str | None = "scripts/dev/update-deps.sh",
    heads: tuple[str, str] = ("a", "a"),
    status: str = "",
    run: Callable[[Path], None] | None = None,
) -> list[list[str]]:
    monkeypatch.setattr(_MOD + ".config.dependency_update_hook", lambda root: hook)  # noqa: ARG005
    head_iter = iter(heads)

    def fake_read(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return next(head_iter)
        if args == ("status", "--porcelain"):
            return status
        raise AssertionError(args)

    monkeypatch.setattr(_MOD + ".git.read_output", fake_read)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> int:
        calls.append(cmd)
        if run is not None:
            run(tmp_path)
        return 0

    monkeypatch.setattr(_MOD + ".progress.run", fake_run)
    return calls


def test_applies_only_with_hook_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "vergil.toml").write_text("")
    monkeypatch.setattr(_MOD + ".config.dependency_update_hook", lambda root: None)  # noqa: ARG005
    assert RepoHookUpdater().applies(_ctx(tmp_path)) is False
    monkeypatch.setattr(_MOD + ".config.dependency_update_hook", lambda root: "h.sh")  # noqa: ARG005
    assert RepoHookUpdater().applies(_ctx(tmp_path)) is True


def test_applies_false_without_vergil_toml(tmp_path: Path) -> None:
    assert RepoHookUpdater().applies(_ctx(tmp_path)) is False


def test_applies_reads_repo_root_when_no_worktree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "vergil.toml").write_text("")
    seen: list[Path] = []

    def fake_hook(root: Path) -> str:
        seen.append(root)
        return "h.sh"

    monkeypatch.setattr(_MOD + ".config.dependency_update_hook", fake_hook)
    assert RepoHookUpdater().applies(_ctx(tmp_path, worktree=False)) is True
    assert seen == [tmp_path]


def test_runs_hook_in_container_with_report_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _setup(monkeypatch, tmp_path, hook="scripts/dev/update-deps.sh --fast")
    RepoHookUpdater().apply(_ctx(tmp_path))
    assert calls == [
        [
            "vrg-container-run",
            "--",
            "env",
            "VRG_DEPS_REPORT=/workspace/.vergil/deps-report.jsonl",
            "scripts/dev/update-deps.sh",
            "--fast",
        ]
    ]


def test_hook_that_edits_reports_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path, status=" M opentofu/x.hcl")
    result = RepoHookUpdater().apply(_ctx(tmp_path))
    assert result.changed is True
    assert result.commit_message == "chore(deps): repo hook"
    assert result.updater == "hook"
    assert result.summary == "repo hook `scripts/dev/update-deps.sh`"


def test_hook_that_does_nothing_reports_no_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path, status="")
    result = RepoHookUpdater().apply(_ctx(tmp_path))
    assert result.changed is False
    assert result.summary == "repo hook `scripts/dev/update-deps.sh` (no changes)"
    assert result.held_back == []


def test_hook_report_entries_collected_and_file_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = HeldBack(source="vergil-vm", name="lima", current="2.0.0", latest="3.0.1")

    def write_report(root: Path) -> None:
        (root / ".vergil" / "deps-report.jsonl").write_text(report.to_line(entry) + "\n")

    _setup(monkeypatch, tmp_path, run=write_report)
    result = RepoHookUpdater().apply(_ctx(tmp_path))
    assert result.held_back == [entry]
    assert not (tmp_path / ".vergil" / "deps-report.jsonl").exists()


def test_stale_report_is_truncated_before_the_hook_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    stale = HeldBack(source="old", name="old", current="1", latest="2")
    (tmp_path / ".vergil").mkdir()
    (tmp_path / ".vergil" / "deps-report.jsonl").write_text(report.to_line(stale) + "\n")
    _setup(monkeypatch, tmp_path)
    result = RepoHookUpdater().apply(_ctx(tmp_path))
    assert result.held_back == []


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            subprocess.CalledProcessError(2, ["h"], output="tofu: registry unreachable"),
            "tofu: registry unreachable",
        ),
        (
            subprocess.CalledProcessError(
                2, ["h"], output="stdout text", stderr="stderr: registry unreachable"
            ),
            "stderr: registry unreachable",
        ),
        (subprocess.CalledProcessError(2, ["h"]), ""),
    ],
)
def test_hook_nonzero_exit_fails_loud_with_output(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exc: subprocess.CalledProcessError,
    expected: str,
) -> None:
    def boom(root: Path) -> None:  # noqa: ARG001
        raise exc

    _setup(monkeypatch, tmp_path, run=boom)
    with pytest.raises(UpdateDepsError, match=r"hook failed \(exit 2\)") as info:
        RepoHookUpdater().apply(_ctx(tmp_path))
    assert info.value.detail == expected
    assert info.value.phase == "update"


def test_hook_that_commits_fails_head_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _setup(monkeypatch, tmp_path, heads=("aaaa1111", "bbbb2222"))
    with pytest.raises(UpdateDepsError, match="moved HEAD") as info:
        RepoHookUpdater().apply(_ctx(tmp_path))
    assert "aaaa1111 -> bbbb2222" in str(info.value)


def test_hook_malformed_report_line_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def bad(root: Path) -> None:
        (root / ".vergil" / "deps-report.jsonl").write_text("{oops\n")

    _setup(monkeypatch, tmp_path, run=bad)
    with pytest.raises(UpdateDepsError, match="line 1"):
        RepoHookUpdater().apply(_ctx(tmp_path))


def test_hook_unset_at_apply_time_fails_loud(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _setup(monkeypatch, tmp_path, hook=None)
    with pytest.raises(UpdateDepsError, match=r"\[dependency-update\]\.hook is not set"):
        RepoHookUpdater().apply(_ctx(tmp_path))
    assert calls == []
